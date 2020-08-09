#!/bin/python
# -*- coding: utf-8 -*-

import numpy as np
import numpy.linalg as nl
import time
from .parser import DSGE as dsge
from numba import njit

aca = np.ascontiguousarray


@njit(cache=True, nogil=True)
def preprocess_jit(A, N, J, cx, x_bar, l_max, k_max):
    """jitted preprocessing of system matrices until (l_max, k_max)
    """

    # these must be the real max values, not only the size of the matrices
    l_max += 1
    k_max += 1

    dimp, dimq = J.shape
    s_max = l_max + k_max

    mat = np.empty((l_max, k_max, s_max, dimq, dimq))
    term = np.empty((l_max, k_max, s_max, dimq))
    core_mat = np.empty((l_max, s_max, dimq, dimq))
    core_term = np.empty((s_max, dimq))

    core_mat[0, 0, :] = np.identity(dimq)
    res = np.zeros((dimq, dimq))

    for s in range(s_max):

        if s:
            core_mat[0, s, :] = core_mat[0, s-1, :] @ N
            res += core_mat[0, s-1, :]

        core_term[s, :] = res @ cx

        for l in range(1, l_max):

            core_mat[l, s, :] = core_mat[l-1, s, :] @ A

    for l in range(l_max):

        for k in range(k_max):

            JN = J @ core_mat[l, k]
            sterm = J @ core_term[k]

            core = -nl.inv(JN[:, :dimp])

            SS_mat = core @ aca(JN)
            SS_term = core @ sterm

            for s in range(s_max):

                doit = True
                l0 = s
                k0 = 0
                s0 = 0

                if s > l:
                    l0 = l
                    if s > l+k+1:
                        continue
                    elif s == l+k+1:
                        k0 = k
                        s0 = 1
                    else:
                        k0 = s-l
                        s0 = 0

                matrices = core_mat[l0, k0]
                oterm = core_term[k0]

                fin_mat = aca(matrices[:, :dimp]) @ SS_mat + aca(matrices)
                fin_term = aca(matrices[:, :dimp]) @ SS_term + oterm

                mat[l, k, s], term[l, k, s] = core_mat[s0,
                                                       0] @ fin_mat, core_mat[s0, 0] @ fin_term

    return mat, term


def preprocess(self, verbose):
    """dispatcher to jitted preprocessing
    """

    l_max, k_max = self.lks
    A, N, J, cc, x_bar, ff, S, aux = self.sys

    cx = cc * x_bar

    st = time.time()
    self.precalc_mat = preprocess_jit(A, N, J, cx, x_bar, l_max, k_max)

    if verbose:
        print('[preprocess:]'.ljust(
            15, ' ')+' Preprocessing finished within %ss.' % np.round((time.time() - st), 3))

    return


@njit(nogil=True, cache=True)
def find_lk(mat, term, dimp, ff, x_bar, l_max, k_max, q):
    """iteration loop to find (l,k) given state q
    """

    flag = 0
    l, k = 0, 0

    # check if (0,0) is a solution
    while check_cnst(mat, term, dimp, ff, l, l, 0, q) - x_bar > 0:
        l += 1
        if l == l_max:
            break

    # check if (0,0) is a solution
    if l < l_max:
        # needs to be wrapped so that both loops can be exited at once
        l, k = bruite_wrapper(mat, term, dimp, ff, x_bar, l_max, k_max, q)

        # if still no solution, use approximation
        if l == 999:
            flag = 1
            l, k = 0, 0
            while check_cnst(mat, term, dimp, ff, l+k, l, k, q) - x_bar > 0:
                k += 1
                if k >= k_max:
                    # set error flag 'no solution + k_max reached'
                    flag = 2
                    break

    if not k:
        l = 1

    return l, k, flag


@njit(cache=True, nogil=True)
def t_func_jit(mat, term, dimp, ff, x_bar, S, aux, l_max, k_max, state, set_k):
    """jitted transitiona function
    """

    # state given in y-space, translate to x-space
    x = aux @ state

    p0 = x[:dimp]
    q0 = x[dimp:]

    if set_k == -1:
        # find (l,k) if requested
        l, k, flag = find_lk(mat, term, dimp, ff, x_bar, l_max, k_max, q0)
    else:
        l, k = int(not bool(set_k)), set_k
        flag = 0

    x0 = aca(mat[l, k, 0][:, dimp:]) @ q0 + term[l, k, 0]  # x(-1)
    x = aca(mat[l, k, 1]) @ x0 + term[l, k, 1]  # x
    x1 = aca(mat[l, k, 2]) @ x0 + term[l, k, 2]  # x(+1)

    # translate back to y
    newstate = S @ np.hstack((x1[dimp:], x, x0, p0))

    return newstate, l, k, flag


@njit(nogil=True, cache=True)
def check_cnst(mat, term, dimp, ff, s, l, k, q0):
    """constraint value in period s given CDR-state q0 under the assumptions (l,k)
    """

    x = aca(mat[l, k, 0][:, dimp:]) @ q0 + term[l, k, 0]
    q = (aca(mat[l, k, s+1]) @ x + term[l, k, s+1])[dimp:]

    if s:
        x = aca(mat[l, k, s]) @ x + term[l, k, s]

    return ff[0] @ q + ff[1] @ x


@njit(nogil=True, cache=True)
def bruite_wrapper(mat, term, dimp, ff, x_bar, l_max, k_max, q):
    """iterate over (l,k) until (l_max, k_max) and check if RE equilibrium
    """

    for l in range(l_max):
        for k in range(1, k_max):
            if l:
                if check_cnst(mat, term, dimp, ff, 0, l, k, q) - x_bar < 0:
                    continue
                if l > 1:
                    if check_cnst(mat, term, dimp, ff, l-1, l, k, q) - x_bar < 0:
                        continue
            if check_cnst(mat, term, dimp, ff, k+l, l, k, q) - x_bar < 0:
                continue
            if check_cnst(mat, term, dimp, ff, l, l, k, q) - x_bar > 0:
                continue
            if k > 1:
                if check_cnst(mat, term, dimp, ff, k+l-1, l, k, q) - x_bar > 0:
                    continue
            return l, k

    return 999, 999


def solve_p_cont(sys, evs, l, k, q):
    """for continuous (l,k). Not in use for pydsge
    """

    A, N, JJ, cc, x_bar, ff, S, aux = sys
    vra, wa, vla, vrn, wn, vln = evs
    J = JJ.astype(np.complex128)

    dimx = J.shape[1]
    dimp = J.shape[0]

    mat = J @ vln*wn**k @ vrn @ vla*wa**l @ vra

    ding = J @ nl.inv(np.eye(dimx) - N).astype(np.complex128)

    term = ding @ (np.eye(dimx) - vln*wn**k @ vrn) @ (cc *
                                                      x_bar).astype(np.complex128)

    return -np.real(nl.inv(mat[:, :dimp]) @ (term + mat[:, dimp:] @ q.astype(np.complex128)))


def move_q_cont(sys, evs, s, l, k, p, q):
    """for continuous (l,k). Not in use for pydsge
    """

    A, N, J, cc, x_bar, ff, S, aux = sys
    vra, wa, vla, vrn, wn, vln = evs

    dimx = J.shape[1]
    dimp = J.shape[0]

    pre = vla*wa**max(s-k-l, 0) @ vra
    t1 = pre @ vln*wn**min(max(s-l, 0), k) @ vrn @ vla*wa**min(s, l) @ vra
    ding = pre @ nl.inv(np.eye(dimx) - N).astype(np.complex128)

    t2 = ding @ (np.eye(dimx) - vln*wn ** min(max(s-l, 0), k)
                 @ vrn) @ (cc * x_bar).astype(np.complex128)
    # t2 = pre @ nl.inv(np.eye(dimx) - N) @ (np.eye(dimx) - vln*wn** min(max(s-l,0),k) @ vrn) @ cc * x_bar

    return np.real(t1[:, :dimp] @ p.astype(np.complex128) + t1[:, dimp:] @ q.astype(np.complex128) + t2)
