# encoding: utf-8
# Released under The MIT License (MIT)
# http://opensource.org/licenses/MIT
# Copyright (c) 2013-2016 SCoT Development Team

"""Vector autoregressive (VAR) model."""

from __future__ import division

import numpy as np
import scipy as sp

from . import config
from . import xvschema as xv
from scot.datatools import acm
from .datatools import cat_trials, atleast_3d
from .parallel import parallel_loop
from .utils import check_random_state


class Defaults(object):
    xvschema = xv.multitrial


class VARBase(object):
    """Represents a vector autoregressive (VAR) model.

    .. warning:: `VARBase` is an abstract class that defines the interface for
    VAR model implementations. Several methods must be implemented by derived
    classes.

    Parameters
    ----------
    model_order : int
        Autoregressive model order.
    n_jobs : int | None, optional
        Number of jobs to run in parallel for various tasks (e.g. whiteness
        testing). If set to None, joblib is not used at all. Note that the main
        script must be guarded with `if __name__ == '__main__':` when using
        parallelization.
    verbose : bool | None, optional
        Whether to print information to stdout. The default is None, which
        means the verbosity setting from the global configuration is used.

    Notes
    -----
    *b* is of shape [m, m*p], with sub matrices arranged as follows:

    +------+------+------+------+
    | b_00 | b_01 | ...  | b_0m |
    +------+------+------+------+
    | b_10 | b_11 | ...  | b_1m |
    +------+------+------+------+
    | ...  | ...  | ...  | ...  |
    +------+------+------+------+
    | b_m0 | b_m1 | ...  | b_mm |
    +------+------+------+------+

    Each sub matrix b_ij is a column vector of length p that contains the
    filter coefficients from channel j (source) to channel i (sink).
    """

    def __init__(self, model_order, n_jobs=1, verbose=None):
        self.p = model_order
        self.coef = None
        self.residuals = None
        self.rescov = None
        self.n_jobs = n_jobs
        if verbose is None:
            self.verbose = config.getboolean('scot', 'verbose')
        else:
            self.verbose = verbose

    def copy(self):
        """Create a copy of the VAR model."""
        other = self.__class__(self.p)
        other.coef = self.coef.copy()
        other.residuals = self.residuals.copy()
        other.rescov = self.rescov.copy()
        return other

    def fit(self, data):
        """Fit VAR model to data.

        .. warning:: This function must be implemented by derived classes.

        Parameters
        ----------
        data : array, shape (trials, channels, samples) or (channels, samples)
            Epoched or continuous data set.

        Returns
        -------
        self : :class:`VAR`
            The :class:`VAR` object to facilitate method chaining (see usage
            example).
        """
        raise NotImplementedError('method fit() is not implemented in ' +
                                  str(self))

    def optimize(self, data):
        """Optimize model fitting hyperparameters (e.g. regularization).

        .. warning:: This function must be implemented by derived classes.

        Parameters
        ----------
        data : array, shape (trials, channels, samples) or (channels, samples)
            Epoched or continuous data set.
        """
        raise NotImplementedError('method optimize() is not implemented in ' +
                                  str(self))

    def from_yw(self, acms):
        """Determine VAR model from autocorrelation matrices by solving the
        Yule-Walker equations.

        Parameters
        ----------
        acms : array, shape (n_lags, n_channels, n_channels)
            acms[l] contains the autocorrelation matrix at lag l. The highest
            lag must equal the model order.

        Returns
        -------
        self : :class:`VAR`
            The :class:`VAR` object to facilitate method chaining (see usage
            example).
        """
        if len(acms) != self.p + 1:
            raise ValueError("Number of autocorrelation matrices ({}) does not"
                             " match model order ({}) + 1.".format(len(acms),
                                                                   self.p))

        n_channels = acms[0].shape[0]

        acm = lambda l: acms[l] if l >= 0 else acms[-l].T

        r = np.concatenate(acms[1:], 0)

        rr = np.array([[acm(m-k) for k in range(self.p)]
                      for m in range(self.p)])
        rr = np.concatenate(np.concatenate(rr, -2), -1)

        c = sp.linalg.solve(rr, r)

        # calculate residual covariance
        r = acm(0)
        for k in range(self.p):
            bs = k * n_channels
            r -= np.dot(c[bs:bs + n_channels, :].T, acm(k + 1))

        self.coef = np.concatenate([c[m::n_channels, :]
                                    for m in range(n_channels)]).T
        self.rescov = r
        return self

    def simulate(self, l, noisefunc=None, random_state=None):
        """Simulate vector autoregressive (VAR) model.

        This function generates data from the VAR model.

        Parameters
        ----------
        l : int or [int, int]
            Number of samples to generate. Can be a tuple or list, where l[0]
            is the number of samples and l[1] is the number of trials.
        noisefunc : func, optional
            This function is used to create the generating noise process. If
            set to None, Gaussian white noise with zero mean and unit variance
            is used.

        Returns
        -------
        data : array, shape (n_trials, n_samples, n_channels)
            Generated data.
        """
        m, n = np.shape(self.coef)
        p = n // m

        try:
            l, t = l
        except TypeError:
            t = 1

        if noisefunc is None:
            rng = check_random_state(random_state)
            noisefunc = lambda: rng.normal(size=(1, m))

        n = l + 10 * p

        y = np.zeros((n, m, t))
        res = np.zeros((n, m, t))

        for s in range(t):
            for i in range(p):
                e = noisefunc()
                res[i, :, s] = e
                y[i, :, s] = e
            for i in range(p, n):
                e = noisefunc()
                res[i, :, s] = e
                y[i, :, s] = e
                for k in range(1, p + 1):
                    y[i, :, s] += self.coef[:, (k - 1)::p].dot(y[i - k, :, s])

        self.residuals = res[10 * p:, :, :].T
        self.rescov = np.cov(cat_trials(self.residuals).T, rowvar=False)

        return y[10 * p:, :, :].transpose([2, 1, 0])

    def predict(self, data):
        """Predict samples on actual data.

        The result of this function is used for calculating the residuals.

        Parameters
        ----------
        data : array, shape (trials, channels, samples) or (channels, samples)
            Epoched or continuous data set.

        Returns
        -------
        predicted : array, shape `data`.shape
            Data as predicted by the VAR model.

        Notes
        -----
        Residuals are obtained by r = x - var.predict(x)
        """
        data = atleast_3d(data)
        t, m, l = data.shape

        p = int(np.shape(self.coef)[1] / m)

        y = np.zeros(data.shape)
        if t > l - p:  # which takes less loop iterations
            for k in range(1, p + 1):
                bp = self.coef[:, (k - 1)::p]
                for n in range(p, l):
                    y[:, :, n] += np.dot(data[:, :, n - k], bp.T)
        else:
            for k in range(1, p + 1):
                bp = self.coef[:, (k - 1)::p]
                for s in range(t):
                    y[s, :, p:] += np.dot(bp, data[s, :, (p - k):(l - k)])

        return y

    def is_stable(self):
        """Test if VAR model is stable.

        This function tests stability of the VAR model as described in [1]_.

        Returns
        -------
        out : bool
            True if the model is stable.

        References
        ----------
        .. [1] H. Lütkepohl, "New Introduction to Multiple Time Series
               Analysis", 2005, Springer, Berlin, Germany.
        """
        m, mp = self.coef.shape
        p = mp // m
        assert(mp == m * p)  # TODO: replace with raise?

        top_block = []
        for i in range(p):
            top_block.append(self.coef[:, i::p])
        top_block = np.hstack(top_block)

        im = np.eye(m)
        eye_block = im
        for i in range(p - 2):
            eye_block = sp.linalg.block_diag(im, eye_block)
        eye_block = np.hstack([eye_block, np.zeros((m * (p - 1), m))])

        tmp = np.vstack([top_block, eye_block])

        return np.all(np.abs(np.linalg.eig(tmp)[0]) < 1)

    def test_whiteness(self, h, repeats=100, get_q=False, random_state=None):
        """Test if VAR model residuals are white (up to a lag of h).

        This function calculates the Li-McLeod Portmanteau test statistic Q to
        test against the null hypothesis H0 (the residuals are white) [1]_.
        Surrogate data for H0 is created by sampling from random permutations
        of the residuals.

        Usually, the returned p-value is compared against a pre-defined type I
        error level of alpha=0.05 or alpha=0.01. If p<=alpha, the hypothesis of
        white residuals is rejected, which indicates that the VAR model does
        not adequately describe the data.

        Parameters
        ----------
        h : int
            Maximum lag that is included in the test statistic.
        repeats : int, optional
            Number of samples to create under the null hypothesis.
        get_q : bool, optional
            Return Q statistic along with p-value.

        Returns
        -------
        pr : float
            Probability of observing a more extreme value of Q under the
            assumption that H0 is true.
        q0 : list of float, optional (`get_q`)
            Individual surrogate estimates that were used for estimating the
            distribution of Q under H0.
        q : float, optional (`get_q`)
            Value of the Q statistic of the residuals

        Notes
        -----
        According to [2]_, h must satisfy h = O(n^0.5), where n is the length
        (time samples) of the residuals.

        References
        ----------
        .. [1] H. Lütkepohl, "New Introduction to Multiple Time Series
               Analysis", 2005, Springer, Berlin, Germany.
        .. [2] J.R.M. Hosking, "The Multivariate Portmanteau Statistic", 1980,
               J. Am. Statist. Assoc.
        """

        return test_whiteness(self.residuals, h=h, p=self.p, repeats=repeats,
                              get_q=get_q, n_jobs=self.n_jobs,
                              verbose=self.verbose, random_state=random_state)

    def _construct_eqns(self, data):
        """Construct VAR equation system.
        """
        return _construct_var_eqns(data, self.p)


def _construct_var_eqns(data, p, delta=None):
        """Construct VAR equation system (optionally with RLS constraint).
        """
        t, l, m = np.shape(data)
        n = (l - p) * t  # number of linear relations
        rows = n if delta is None else n + m * p

        # Construct matrix x (predictor variables)
        x = np.zeros((rows, m * p))
        for i in range(m):
            for k in range(1, p + 1):
                x[:n, i * p + k - 1] = np.reshape(data[:, p - k:-k, i], n)
        if delta is not None:
            np.fill_diagonal(x[n:, :], delta)

        # Construct vectors yi (response variables for each channel i)
        y = np.zeros((rows, m))
        for i in range(m):
            y[:n, i] = np.reshape(data[:, p:, i], n)

        return x, y


def test_whiteness(data, h, p=0, repeats=100, get_q=False, n_jobs=1,
                   verbose=0, random_state=None):
    """Test if signals are white (serially uncorrelated up to a lag of h).

    This function calculates the Li-McLeod Portmanteau test statistic Q to test
    against the null hypothesis H0 (the residuals are white) [1]_.
    Surrogate data for H0 is created by sampling from random permutations of
    the residuals.

    Usually, the returned p-value is compared against a pre-defined type I
    error level of alpha=0.05 or alpha=0.01. If p<=alpha, the hypothesis of
    white residuals is rejected, which indicates that the VAR model does not
    adequately describe the data.

    Parameters
    ----------
    data : array, shape (trials, channels, samples) or (channels, samples)
        Epoched or continuous data set.
    h : int
        Maximum lag that is included in the test statistic.
    p : int, optional
        Model order (if `data` are the residuals resulting from fitting a VAR
        model).
    repeats : int, optional
        Number of samples to create under the null hypothesis.
    get_q : bool, optional
        Return Q statistic along with *p*-value
    n_jobs : int | None, optional
        Number of jobs to run in parallel. If set to None, joblib is not used
        at all. See `joblib.Parallel` for details.
    verbose : int
        Verbosity level passed to joblib.

    Returns
    -------
    pr : float
        Probability of observing a more extreme value of Q under the assumption
        that H0 is true.
    q0 : list of float, optional (`get_q`)
        Individual surrogate estimates that were used for estimating the
        distribution of Q under H0.
    q : float, optional (`get_q`)
        Value of the Q statistic of the residuals.

    Notes
    -----
    According to [2]_, h must satisfy h = O(n^0.5), where n is the length (time
    samples) of the residuals.

    References
    ----------
    .. [1] H. Lütkepohl, "New Introduction to Multiple Time Series Analysis",
           2005, Springer, Berlin, Germany.
    .. [2] J.R.M. Hosking, "The Multivariate Portmanteau Statistic", 1980, J.
           Am. Statist. Assoc.
    """
    res = data[:, :, p:]
    t, m, n = res.shape
    nt = (n - p) * t

    q0 = _calc_q_h0(repeats, res, h, nt, n_jobs, verbose,
                    random_state=random_state)[:, 2, -1]
    q = _calc_q_statistic(res, h, nt)[2, -1]

    # probability of observing a result more extreme than q
    # under the null-hypothesis
    pr = np.sum(q0 >= q) / repeats

    if get_q:
        return pr, q0, q
    else:
        return pr


def _calc_q_statistic(x, h, nt):
    """Calculate Portmanteau statistics up to a lag of h.
    """
    t, m, n = x.shape

    # covariance matrix of x
    c0 = acm(x, 0)

    # LU factorization of covariance matrix
    c0f = sp.linalg.lu_factor(c0, overwrite_a=False, check_finite=True)

    q = np.zeros((3, h + 1))
    for l in range(1, h + 1):
        cl = acm(x, l)

        # calculate tr(cl' * c0^-1 * cl * c0^-1)
        a = sp.linalg.lu_solve(c0f, cl)
        b = sp.linalg.lu_solve(c0f, cl.T)
        tmp = a.dot(b).trace()

        # Box-Pierce
        q[0, l] = tmp

        # Ljung-Box
        q[1, l] = tmp / (nt - l)

        # Li-McLeod
        q[2, l] = tmp

    q *= nt
    q[1, :] *= (nt + 2)

    q = np.cumsum(q, axis=1)

    for l in range(1, h+1):
        q[2, l] = q[0, l] + m * m * l * (l + 1) / (2 * nt)

    return q


def _calc_q_h0(n, x, h, nt, n_jobs=1, verbose=0, random_state=None):
    """Calculate q under the null hypothesis of whiteness.
    """
    rng = check_random_state(random_state)
    par, func = parallel_loop(_calc_q_statistic, n_jobs, verbose)
    q = par(func(rng.permutation(x.T).T, h, nt) for _ in range(n))
    
    return np.array(q)
