import itertools
import time
import random

import numpy as np
import pandas as pd
from pandas.api.types import is_categorical_dtype

import torch
import torch.optim as optim

from brmp import makedesc
from brmp.formula import parse, OrderedSet, unique
from brmp.design import Metadata, makedata, metadata_from_cols, RealValued, Categorical, Integral, code_lengths
from brmp.family import Normal
from brmp.backend import data_from_numpy
from brmp.pyro_backend import backend as pyro_backend
from brmp.fit import Fit

from brmp.oed.nets import QIndep, QFull # noqa: 401


# Provides a convenient interface for performing sequential OED.

# Also acts as a single data structure in which to store the
# components of the model definition. (Formula, family, priors, etc.)
# The constructor takes care of the boiler plate required to set-up a
# brmp model.

# Also holds the data-so-far. This data is used when computing the
# next trial. The data-so-far can be extended with the result of an
# experiment using the `add_result` method. Note that `data_so_far` is
# the only mutable state held by instances of SequentialOED. The only
# method that modifies this is `add_result`.

# There are also methods/properties for obtaining information about
# the current sequence:

# oed.design_space()
# oed.data_so_far

def null(*args):
    return None


class SequentialOED:
    def __init__(self, formula_str, cols, family=Normal, priors=[],
                 contrasts={}, target_coefs=[], backend=pyro_backend):
        formula = parse(formula_str)
        metadata = metadata_from_cols(cols)
        model_desc = makedesc(formula, metadata, family, priors, code_lengths(contrasts))
        model = backend.gen(model_desc)
        data_so_far = empty_df_from_cols(cols)
        dscols = design_space_cols(formula, metadata)

        assert type(target_coefs) == list
        all_population_level_coefs = ['b_{}'.format(coef) for coef in model_desc.population.coefs]
        if len(target_coefs) == 0:
            target_coefs = all_population_level_coefs
        else:
            # TODO: Move `unique` to utils or similar.
            target_coefs = unique(target_coefs)
            assert set(target_coefs).issubset(set(all_population_level_coefs)), 'unknown target coefficient given'

        num_coefs = len(target_coefs)

        # TODO: Prefix non-public stuff with underscores?
        self.formula = formula
        self.contrasts = contrasts
        self.metadata = metadata
        self.model_desc = model_desc
        self.model = model
        self.data_so_far = data_so_far
        self.num_coefs = num_coefs
        self.target_coefs = target_coefs
        self.dscols = dscols

        self.backend = backend
        self.num_samples = 1000

    def next_trial(self, callback=None, verbose=False, design_space=None, fixed_target_interval=True, seed=None):
        assert (design_space is None or
                type(design_space) == list and all(type(t) == dict for t in design_space))

        if callback is None:
            callback = null

        if design_space is None:
            design_space = full_design_space(self.dscols, self.metadata)
        else:
            sanity_check_design_space(design_space, self.dscols, self.metadata)
        assert len(design_space) > 0, 'design space cannot be empty'

        design_space_df = design_space_to_df(self.dscols, design_space, self.metadata)

        # Code the data-so-far data frame into design matrices.
        dsf = data_from_numpy(self.backend,
                              makedata(self.formula, self.data_so_far, self.metadata, self.contrasts))

        # Draw samples from current distribution over parameters.
        if len(self.data_so_far) == 0:
            samples = self.backend.prior(dsf, self.model, num_samples=self.num_samples, seed=seed)
        else:
            samples = self.backend.nuts(dsf, self.model, iter=self.num_samples,
                                        warmup=self.num_samples // 2, num_chains=1, seed=seed)
        fit = Fit(self.formula, self.metadata, self.contrasts, dsf, self.model_desc, self.model, samples, self.backend)

        # Values sampled for (population-level) target coefs. (numpy array.)
        latent_samples = [fit.get_scalar_param(tc) for tc in self.target_coefs]

        # Draw samples from p(y|theta;d)
        y_samples = fit.fitted('sample', design_space_df)  # numpy array.

        # TODO: Given the correlation between consecutive samples, we
        # can presumably train on thinned samples and not loose
        # anything/much? If so, can we use number of effective samples
        # or similar to decide how much thinning to apply?

        # All ANN work is done using PyTorch, so convert samples from
        # numpy to torch ready for what follows.
        latent_samples = torch.stack([torch.tensor(col) for col in latent_samples], 1)
        y_samples = torch.tensor(y_samples)
        assert latent_samples.shape == (self.num_samples, self.num_coefs)
        assert y_samples.shape == (self.num_samples, len(design_space))

        # Compute the targets. (These are used by all designs.)

        # TODO: How might we set this automatically, and possibly
        # adapt it over time? Is setting it so that ~50% of sample are
        # within the interval sensible? Or perhaps at least ensure
        # that the class are reasonably balanced. 70/30 might be OK,
        # 99/1 probably not?

        # Here's one idea: (TODO: Test this.)

        # Determine the interval to use for targets.
        if fixed_target_interval:
            eps = 0.5
            interval_low = -eps
            interval_high = eps
        else:
            # qs is a 2 x num_coefs array. The first row is
            # per-coefficient 0.25 quantiles, the second row 0.75.
            qs = np.quantile(latent_samples, [.25, .75], 0)
            interval_low = torch.tensor(qs[0])
            interval_high = torch.tensor(qs[1])

        print('Targets interval:')
        print('  Low:  {}'.format(interval_low))
        print('  High: {}'.format(interval_high))

        targets = ((interval_low < latent_samples) & (latent_samples < interval_high)).long()
        assert targets.shape == (self.num_samples, self.num_coefs)
        print('Targets class balance: {}'.format(targets.float().mean(0)))

        inputs = y_samples.t().unsqueeze(-1)
        assert inputs.shape == (len(design_space), self.num_samples, 1)

        # Estimate EIGs
        Q = QFull  # QIndep
        vectorize = True
        est_eig_fn = est_eig_vec if vectorize else est_eig
        eigs, cbvals, elapsed = est_eig_fn(Q, targets, inputs, design_space, self.target_coefs, callback, verbose)
        if verbose:
            print('Elapsed: {}'.format(elapsed))

        dstar = argmax(eigs)
        return design_space[dstar], dstar, list(zip(design_space, eigs)), fit, cbvals

    def add_result(self, design, result):
        assert type(design) == dict
        assert set(design.keys()) == set(self.dscols)
        assert type(result) == float
        row = dict({self.formula.response: result}, **design)
        self.data_so_far = df_append_row(self.data_so_far, row)


def argmax(lst):
    return torch.argmax(torch.tensor(lst)).item()


# Estimate the EIG for each design.
def est_eig(Q, targets, inputs, design_space, target_coefs, callback, verbose):
    num_coefs = targets.shape[1]
    eigs = []
    cbvals = []
    elapsed = 0.0
    targets_enc = Q.encode(targets).unsqueeze(0)
    for i, design in enumerate(design_space):
        inputs_i = inputs[i].unsqueeze(0)

        # Construct and optimised the network.
        q_net = Q(num_coefs, num_designs=1)
        t0 = time.time()
        optimise(q_net, inputs_i, targets_enc, verbose)

        eig = torch.mean(q_net.logprobs(inputs_i, targets_enc)).item()
        eigs.append(eig)
        elapsed += (time.time() - t0)

        cbvals.append(callback(q_net, inputs_i, targets, [design], target_coefs)[0])

    return eigs, cbvals, elapsed


# Estimate the EIG for each design. (Vectorized over designs.)
def est_eig_vec(Q, targets, inputs, design_space, target_coefs, callback, verbose):
    num_coefs = targets.shape[1]
    # Encode targets, and replicate for each design.
    targets_enc = Q.encode(targets).unsqueeze(0).expand(len(design_space), -1, -1)
    q_net = Q(num_coefs, len(design_space))
    t0 = time.time()
    optimise(q_net, inputs, targets_enc, verbose)
    eigs = torch.mean(q_net.logprobs(inputs, targets_enc), -1)
    elapsed = time.time() - t0
    cbvals = callback(q_net, inputs, targets, design_space, target_coefs)
    return eigs.tolist(), cbvals, elapsed


def optimise(net, inputs, targets, verbose):

    assert inputs.shape[1] == targets.shape[1]
    N = inputs.shape[1]
    batch_size = 50
    assert N % batch_size == 0

    # TODO: Note: using some weight decay probably makes sense here.

    # Shuffle data.
    ix = list(range(N))
    random.shuffle(ix)
    inputs = inputs[:, ix, :]
    targets = targets[:, ix, :]

    # Form mini-batches.
    num_batches = N // batch_size
    input_batches = inputs.chunk(num_batches, 1)
    target_batches = targets.chunk(num_batches, 1)

    optimizer = optim.Adam(net.parameters(), lr=0.01)

    num_epochs = 50
    for i in range(num_epochs):
        epoch_loss = 0.
        for j in range(num_batches):
            optimizer.zero_grad()
            loss = -torch.sum(torch.sum(net.logprobs(input_batches[j], target_batches[j]), -1))
            epoch_loss += loss.item()
            loss.backward()
            optimizer.step()
        if (i+1) % 10 == 0 and verbose:
            print('{:5d} | {:.6f}'.format(i+1, epoch_loss / N))

    if verbose:
        print('--------------------')


def empty_df_from_cols(cols):
    def emptydfcol(col):
        if type(col) == Categorical:
            return pd.Categorical([])
        elif type(col) == RealValued:
            return []
        elif type(col) == Integral:
            return pd.Series([], dtype=int)
        else:
            raise Exception('encountered unsupported column type')
    return pd.DataFrame({col.name: emptydfcol(col) for col in cols})


# Extract the names of the columns/factors appearing on RHS of the
# model formula.

# TODO: This is similar to `allfactors` in formula.py -- consolidate?

def design_space_cols(formula, meta):
    cols = OrderedSet()
    for t in formula.terms:
        cols = cols.union(t.factors)
    for group in formula.groups:
        cols = cols.union(OrderedSet(*group.columns))
        for t in group.terms:
            cols = cols.union(t.factors)
    assert all(finite(meta.column(c)) for c in cols)
    return list(cols)


def finite(col):
    return (type(col) == Categorical or
            type(col) == Integral and col.min is not None and col.max is not None)


# Compute the product of the sets of possible values taken on by each
# column named in `names`.
def full_design_space(names, metadata):
    assert type(names) == list
    assert all(type(name) == str for name in names)
    assert type(metadata) == Metadata
    # A list of tuples, where each tuple holds a value for each of the
    # design columns.
    designs = list(itertools.product(*[possible_values(metadata.column(name)) for name in names]))
    # Include explicit column names in the design representation,
    # rather than relying implicitly on order.
    return [dict(zip(names, design)) for design in designs]


def possible_values(col):
    assert type(col) in (Categorical, Integral)
    if type(col) == Categorical:
        return col.levels
    elif type(col) == Integral:
        return range(col.min, col.max + 1)
    else:
        raise Exception('unexpected column type')


def sanity_check_design_space(design_space, dscols, metadata):
    lookup = {name: set(possible_values(metadata.column(name)))
              for name in dscols}
    for design in design_space:
        for (col, value) in design.items():
            assert value in lookup[col], 'invalid design {} given in design space'.format(design)


# TODO: This has some overlap with `empty_df_from_cols` -- consolidate?
def design_space_to_df(dscols, design_space, metadata):

    def identity(x):
        return x

    def dispatch(name):
        col = metadata.column(name)
        if type(col) == Categorical:
            return pd.Categorical
        elif type(col) == Integral:
            return identity
        else:
            raise Exception('unhandled column type')

    df = pd.DataFrame(design_space)
    for name in dscols:
        df[name] = dispatch(name)(df[name])
    return df


def df_append_row(df, row):
    assert type(row) == dict
    row_df = pd.DataFrame({k: pd.Categorical([v]) if is_categorical_dtype(df[k]) else [v]
                           for k, v in row.items()})
    out = df.append(row_df, sort=False)
    # Simply appending a new df produces a new df in which
    # (sometimes!?!) a column that was categorical in the two inputs
    # is not categorical in the output. This tweaks the result to
    # account for that. I don't know why this is happening.
    for k in row.keys():
        if is_categorical_dtype(df[k]) and not is_categorical_dtype(out[k]):
            out[k] = pd.Categorical(out[k])
    return out