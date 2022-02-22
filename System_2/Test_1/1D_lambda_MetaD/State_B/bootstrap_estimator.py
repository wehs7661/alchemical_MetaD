import argparse
import glob
import os
import sys
import time

import matplotlib.pyplot as plt
import natsort
import numpy as np
import pandas as pd
import plumed
import sparse
from matplotlib import rc
from memory_profiler import memory_usage
from prettytable import PrettyTable


def initialize():
    parser = argparse.ArgumentParser(
        description='This code uses block bootstrap to calculate the free energy \
                     as a function of a selected CV from a metadynamics simulation.\
                     By default, this code is for analyzing the data of alchemical \
                     metadynamics simulations. Example command: python bootstrapping.py \
                     -d rep_1 -n 10 100 200 500 1000 2000 5000 -hh HILLS_LAMBDA -t 0.2')
    parser.add_argument('-d',
                        '--dir',
                        nargs='+',
                        help='The directory containing the files to be analyzed.')
    parser.add_argument('-i',
                        '--input',
                        default='plumed_sum_bias.dat',
                        help='The PLUMED input file for summing up the bias of metadynamics.\
                              Default: plumed_sum_bias.dat')
    parser.add_argument('-c',
                        '--CV_name',
                        default='lambda',
                        help='The name of the CV for generating the free energy profile.')
    parser.add_argument('-cc',
                        '--colvar',
                        default='COLVAR',
                        help='The filename of the COLVAR file output by the simulation.')
    parser.add_argument('-hh',
                        '--hills',
                        default=glob.glob('HILLS*'),
                        help='The HILLS file generated by the simulation. Default:\
                              file(s) containing "HILLS".')
    parser.add_argument('-n',
                        '--n_blocks',
                        nargs='+',
                        type=int,
                        help='The min, max and the spacing of the number of blocks. If only one \
                              value is specified, only one free energy profile based on the given \
                              block size will be calculated. If more than 3 values are specified, \
                              multiple free energy profiles will be generated based on all given \
                              values.')
    parser.add_argument('-nb',
                        '--n_bootstraps',
                        type=int,
                        default=200,
                        help='The number of bootstrap iterations.')
    parser.add_argument('-t',
                        '--truncate',
                        type=float,
                        default=0,
                        help='The fraction of the simulation to be truncated.')
    parser.add_argument('-a',
                        '--avg',
                        type=float,
                        default=0.2,
                        help='The fraction of the HILLS data to be averaged when reweighting.')
    parser.add_argument('-s',
                        '--seed',
                        type=int,
                        help='The random seed for bootstrapping. Random seed will not be used if\
                             not specified.')
    parser.add_argument('-f',
                        '--factor',
                        type=float,
                        default=0.02,
                        help='The factor for converting the block size into ps / 1 stride in ps.')
    parser.add_argument('-T',
                        '--temp',
                        type=float,
                        default=298.15,
                        help='The simulation temperature in K.')
    parser.add_argument('-r',
                        '--ref',
                        type=float,
                        help='The reference/benchmark of the free energy difference for calculating \
                              r.m.s Z-score and RMSE if multiple simulations are analyzed')
    args_parse = parser.parse_args()

    return args_parse


class Logging:
    def __init__(self, file_name):
        self.f = file_name

    def logger(self, *args, **kwargs):
        """
        Prints the results on screen and to the file free_energy_results.txt

        Parameters
        ---------
        file_name (str): The file name of the output.
        """
        print(*args, **kwargs)
        with open(self.f, "a") as f:
            print(file=f, *args, **kwargs)


def clear_directory(file_name):
    """
    Deletes the previous outputs before performing data analysis.

    Parameters
    ----------
    file_name (str): The file name of the files to be deleted.
    """
    if '*' in file_name:
        file_name = glob.glob(file_name)
    if type(file_name) == str:
        if os.path.isfile(file_name):
            os.remove(file_name)
    elif type(file_name) == list:
        for i in file_name:
            clear_directory(i)


def read_plumed_output(plumed_output):
    """
    This function modifies the given plumed output file if it is corrupted, meaning that
    there might be some duplicates in the time series having the same time frames. If the
    file is not corrupted, this fucntion does nothing but only read in the data. 

    Parameters
    ----------
    plumed_output (str): The filename of the plumed output (such as HILLS or COLVAR files) to be read
    """
    data_original = plumed.read_as_pandas(plumed_output)
    data = data_original[~data_original["time"].duplicated(keep='last')]  # deduplicate time frames
    data = data.dropna()        # drop N/A in case that there is any
    data = data.reset_index()   # reset the index of the data frame, after this an column "index" will be added
    data = data.drop(columns=["index"])    # drop the index column
    if len(data) == len(data_original):
        # The simulation finished without any timeouts or crashing issues.
        pass    # the plumed output file won't be modified and there will be no backup
    else:
        # the data in the plumed output file will be replaced with the deduplicated time series
        backup = plumed_output + '_backup'
        os.system(f'mv {plumed_output} {backup}')
        plumed.write_pandas(data, plumed_output)
    return data


def block_bootstrap(traj, n_blocks, dt, file_path, B, T=298.15, truncate=0, CV='lambda'):
    """
    Calculates the free energy difference and its uncertainty from an
    alchemical metadynamics using block bootstrp.

    Parameters
    ----------
    traj       (PlumedDataFrame): The trajectory data read from a COLVAR file.
    n_blocks   (int): The number of blocks.
    dt         (float): STRIDE in ps
    file_path  (str): The path of the output file fes*dat
    B          (int): The number of bootstrap iterations
    T          (float): The simulation temperature.
    truncate    (float): The fraction of the time series to be truncated.

    Returns
    -------
    df         (float): The free energy difference between the coupled and uncoupled state.
    df_err     (float): The uncertainty of the free energy difference
    """
    k = 1.38064852E-23   # Boltzmann constant
    N_a = 6.02214076E23  # Avogadro's number
    kT = k * T * N_a / 1000  # kT in kJ/mol

    n = int(len(traj) * (1 - truncate))   # number of data points considered
    # make sure the number of frames is a multiple of nblocks (discard the first few frames)
    n = (n // n_blocks) * n_blocks
    b_size = n / np.array(n_blocks) * dt  # units: ps
    bias = np.array(traj["metad.bias"])
    bias -= np.max(bias)  # avoid overflows
    # shape: (nblocks, nframes in one block), weight for each point
    w = np.exp(bias / kT)[-n:].reshape((n_blocks, -1))

    fes_file = open(file_path, 'w')
    fes_file.write(f'{CV}    f             f_err\n')
    isState, pop, fes, fes_err = [], [], [], []
    for i in range(int(np.max(traj[f"{CV}"])) + 1):
        # 1 if in state i
        isState.append(np.array(traj[f"{CV}"] == i)[-n:].reshape((n_blocks, -1)))  # boolean array (memory-efficient)
        # draw samples from np.arange(n_blocks), size refers the output size
        boot = np.random.choice(n_blocks, size=(B, n_blocks))
        # isState[i][boot]: 3D array, shape of pop: (B,) --> could use a lot of memory when using np.average!
        pop.append(np.sum(np.ma.array(w[boot], mask=[isState[i][boot]]), 
                           axis=(1,2)) / np.sum(sparse.COO(isState[i][boot]), axis=(1, 2)).todense())
        fes_boot = np.log(pop[i])
        fes.append(np.log(np.average(isState[i], weights=w)))
        if np.sum(np.isinf(fes_boot)) > 0:   # in case that there are zeros in pop[i]
            warn_str = f'{np.sum(np.isinf(fes_boot))} out of {B} bootstrap iterations had 0 probability.'
            L.logger('=' * int((len(warn_str) - 9) / 2) + ' Warning ' + '=' * int((len(warn_str) - 9) / 2))
            L.logger(warn_str)
            L.logger('=' * len(warn_str))
            fes_boot[np.isinf(fes_boot)] = float('nan')  # replace inf or -inf with nan
        fes_err.append(np.nanstd(fes_boot))   # ignore nan when calculating std

        # Write fes*dat
        fes_file.write(f'  {i}      {fes[-1]: .6f}    {fes_err[-1]: .6f}\n')

    # Free energy difference (kT) between the coupled and uncoupled states
    df_boot = np.log(pop[0]/pop[-1])
    popA0 = np.average(isState[0], weights=w)  # coupled state
    popB0 = np.average(isState[-1], weights=w)
    df = np.log(popA0 / popB0)
    if np.sum(np.isinf(df_boot)) > 0:   # in case that there are zeros in popA0/popB0
        df_boot[np.isinf(df_boot)] = float('nan')  # replace inf or -inf with nan
    df_err = np.nanstd(df_boot)    # ignore nan when calculating std

    return df, df_err


def average_bias(hills, avg_frac):
    """
    Averages the bias over a certain last portion of the simulation. 

    Parameters
    ----------
    hills     (PlumeDataFrame): The data read from HILLS. 
    avg_frac  (int): The fraction of the simulation that the data will be averaged when reweighting.

    Returns
    ----------
    hills_avg (PlumedDataFrame): The time-averaged bias.
    """
    n0 = int(len(hills) * (1 - avg_frac))   # number of data points considered
    # the weights for the first n0 points are 1
    w = np.hstack((np.ones(n0), np.linspace(1, 0, len(hills) - n0)))
    hills_avg = hills.copy()
    hills_avg.height *= w
    return hills_avg


def plot_gaussian(mu, sigma):
    coef = 1/(sigma * np.sqrt(2 * np.pi))
    x = np.arange(mu - 5 * sigma, mu + 5 * sigma, 0.01)
    y = coef * np.exp(-0.5 * ((x - mu) / sigma) ** 2)
    plt.plot(x, y)


def convert_memory_units(mem):
    """
    mem: RAM memory usage in MB to other units.
    """
    power = 1024
    mem *= power ** 2   # first convert to kB
    n = 0
    power_labels = {0: '', 1: 'k', 2: 'M', 3: 'G', 4: 'T'}
    while mem > power:
        mem /= power
        n += 1
    mem_str = f'{mem: .2f} {power_labels[n]}B'

    return mem_str


if __name__ == "__main__":
    T1 = time.time()
    max_mem_boot, max_mem_avg, max_mem_read = 0, 0, 0
    # Step 1: Parse the arguments and set things up
    rc('font', **{
       'family': 'sans-serif',
       'sans-serif': ['DejaVu Sans'],
       'size': 10
       })
    # Set the font used for MathJax - more on this later
    rc('mathtext', **{'default': 'regular'})
    plt.rc('font', family='serif')

    args = initialize()
    p_input = args.input

    args.dir = natsort.natsorted(args.dir)
    if len(args.dir) > 1 and len(args.n_blocks) == 1:
        df_list, df_err_list = [], []  # if multi_dir
        plt.figure()
    for folder in args.dir:
        t1 = time.time()
        # Note that args.colvar is different from colvar_sum_bias
        colvar_sum_bias = folder + 'COLVAR_SUM_BIAS'

        if len(args.n_blocks) == 1:
            multi_b_size = False
            n_blocks = args.n_blocks
        else:
            multi_b_size = True
            try:
                os.system(f'rm -r {folder}FES')
            except:
                pass
            os.makedirs(folder + 'FES')
            if len(args.n_blocks) == 3:
                n_blocks = np.arange(
                    args.nblocks[0], args.n_blocks[1] + args.n_blocks[2], args.n_blocks[2])
            else:
                n_blocks = args.n_blocks

        if args.seed is not None:
            np.random.seed(args.seed)

        B = args.n_bootstraps
        T = args.temp
        truncate = args.truncate
        avg_frac = args.avg
        seed = str(args.seed)
        factor_s = args.factor

        # Write the argument values to the result files
        if len(n_blocks) == 1:
            result_file = folder + \
                f'fes_results_truncate_{truncate}_nblocks_{n_blocks[0]}_avg_{avg_frac}.txt'
        elif len(n_blocks) > 1:
            result_file = folder + \
                f'fes_results_truncate_{truncate}_nblocks_multi_avg_{avg_frac}.txt'
        if os.path.isfile(result_file):
            os.remove(result_file)
        L = Logging(result_file)
        sec_str = 'Section 1: Parameters for data analysis'
        L.logger(sec_str)
        L.logger('=' * len(sec_str))
        L.logger(f'- Command line: {" ".join(sys.argv)}')
        L.logger(f'- Current working directory: {folder}')
        L.logger(
            f'- Files analyzed/used: {args.input}, {args.hills}, and COLVAR output by the simulation')
        L.logger(f'- Number of blocks: {n_blocks}')
        L.logger(f'- Number of bootstrap iterations: {B}')
        L.logger(f'- Truncated fraction: {truncate}')
        L.logger(f'- Averaged fraction: {avg_frac}')
        L.logger(f'- Random seed: {seed}')
        L.logger(f'- STRIDE in ps: {factor_s}')
        L.logger(f'- Simulation temperature: {args.temp}')

        rm_list = ['bck*', 'COLVAR_SUM_BIAS']
        for i in rm_list:
            clear_directory(folder + i)

        # Step 2: Run the plumed driver for reweighting
        script_path = os.path.abspath(__file__)
        # __file__ might changed accordingly (so we used script_path)
        os.chdir(folder)

        # A quick check of COLVAR output of the plumed driver
        infile = open(p_input, 'r')
        lines = infile.readlines()
        infile.close()

        check = False
        for line in lines:
            if 'COLVAR_SUM_BIAS' in line:
                check = True
        if check is False:
            print('Error: Wrong output COLVAR name. This code requires COLVAR_SUM_BIAS as the output of the plumed driver.')
            sys.exit()

        # Case 1: Using the final bias for reweighting (args.avg == 0)
        if avg_frac == 0:
            print('\n====================== Reminder ======================')
            print('The final bias is used for reweighting.')
            print('The input HILLS file for the plumed driver should be the HILLS file generated by the metadynamics simulation')
            print('The output COLVAR file contains the final bias.')
            print('======================================================\n')
            os.system(f'plumed driver --plumed {p_input} --noatoms')

        # Case 2: Using the time-averaged bias for reweighting (args.avg > 0)
        else:
            print('\n====================== Reminder ======================')
            print('Reminder: The time-averaged bias is used for reweighting.')
            print('The input HILLS file for the plumed driver should be the one generated below.')
            print(
                f'The output COLVAR file contains the bias averaged over the last {avg_frac * 100}% of the simulation.')
            print('======================================================\n')

            #mem_avg, hills_avg = memory_usage(
            #    (average_bias, (plumed.read_as_pandas(args.hills), avg_frac)), retval=True)
            mem_avg, hills_avg = memory_usage(
                (average_bias, (read_plumed_output(args.hills), avg_frac)), retval=True)
            #hills_avg = average_bias(read_plumed_output(args.hills), avg_frac)
            plumed.write_pandas(hills_avg, args.hills + "_modified")

            if np.max(mem_avg) > max_mem_avg:
                max_mem_avg = np.max(mem_avg)

            # A quick check of the HILLS input for the PLUMED input file for the plumed driver
            infile = open(p_input, 'r')
            lines = infile.readlines()
            infile.close()

            check = False
            for line in lines:
                if args.hills + "_modified" in line:
                    check = True
            if check is False:
                print('The PLUMED input file for the plumed driver might use a wrong HILLS input file.')
                sys.exit()

            # Before running the plumed driver, we need to deal with the corrupted COLVAR, if any.
            # Modify the COLVAR as needed in case that the simulation crashed or stpeed before reaching the last time frame.
            mem_read, _ = memory_usage((read_plumed_output, (args.colvar, )), retval=True)
            if np.max(mem_read) > max_mem_read:
                max_mem_read = np.max(mem_read)
            os.system(f'plumed driver --plumed {p_input} --noatoms')

        # For any cases, the COLVAR file to be anayzed should be COLVAR_SUM_BIAS.
        os.chdir(os.path.dirname(script_path))
        mem_read, traj = memory_usage((read_plumed_output, (colvar_sum_bias, )), retval=True)

        # Step 3: Calculate the free energy surface and its uncertainty
        df_results = []
        n = int(len(traj) * (1 - truncate))  # number of data points considered
        n = (n // np.array(n_blocks)) * n_blocks
        b_sizes = n / np.array(n_blocks) * args.factor  # units: ps
        for i in range(len(n_blocks)):
            if multi_b_size is False:  # only one fes_*.dat
                fes_path = f'{folder}/fes_truncate_{truncate}_bsize_{b_sizes[i]}_avg_{avg_frac}.dat'
            else:
                fes_path = f'{folder}FES/fes_bsize_{b_sizes[i]}.dat'

            bootstrap_args = (traj, n_blocks[i], args.factor, fes_path, B)
            bootstrap_kw = {'T': T, 'truncate': truncate, 'CV': args.CV_name}
            mem_boot, df_result = memory_usage((block_bootstrap, bootstrap_args, bootstrap_kw), retval=True)
            df_results.append(df_result)

            if np.max(mem_boot) > max_mem_boot:
                max_mem_boot = np.max(mem_boot)

        df_results = np.array(df_results)

        # Tabulate the results in the results file
        table_data = []
        for i in range(len(b_sizes)):
            table_data.append(
                [f'{n_blocks[i]}', f'{b_sizes[i]: .2f}', f'{df_results[i][0]: .6f}', f'{df_results[i][1]: .6f}'])
        x = PrettyTable()
        x.field_names = [
            '# of blocks', 'Block size (ps)', 'Free energy difference (kT)', 'Uncertainty (kT)']
        x.add_rows(table_data)

        if len(args.dir) > 1 and len(n_blocks) == 1:
            df_list.append(df_results[0][0])
            df_err_list.append(df_results[0][1])
            plot_gaussian(df_results[0][0], df_results[0][1])

        sec_str = '\nSection 2: Results of free energy calculations'
        L.logger(sec_str)
        L.logger('=' * (len(sec_str) - 1))
        L.logger(x)

        # Plot uncertainty as a function of block size
        if len(n_blocks) > 1:
            plt.figure()
            plt.plot(b_sizes, df_results[:, 1], 'x-')
            if np.max(b_sizes)/np.min(b_sizes) > 100:
                plt.xscale("log")
            plt.xlabel('Block size (ps)')
            plt.ylabel('Uncertiainty (kT)')
            plt.title('Uncertainty as a function of block size')
            plt.grid()
            plt.savefig(
                folder + f'df_err_bsize_truncate_{truncate}_avg_{avg_frac}.png', dpi=600)

        sec_str = '\nSection 3: Information about the analysis process'
        L.logger(sec_str)
        L.logger('=' * len(sec_str))
        L.logger(
            f'- Files output by this code: \n  fes*dat, HILLS*_modified, COLVAR_SUM_BIAS, df_err_bsize_truncate_{truncate}_avg_{avg_frac}.png, {result_file.split("/")[-1]}')

        # tabulate and document the maximum memory usage
        table_data = []
        table_data.append(['block_bootstrap', f'{convert_memory_units(max_mem_boot)}'])
        table_data.append(['average_bias', f'{convert_memory_units(max_mem_avg)}'])
        table_data.append(['read_plumed_output', f'{convert_memory_units(max_mem_read)}'])

        x = PrettyTable()
        x.field_names = ['Function name', 'Max memory usage']
        x.add_rows(table_data)
        L.logger('- Memory usage')
        L.logger(x)

        t2 = time.time()
        L.logger(f'- Time elapsed: {t2 - t1: .2f} seconds.')

    # Below are the lines for the Gaussian plot
    if len(args.dir) > 1 and len(n_blocks) == 1:
        plt.xlabel('Free energy difference ($ k_{B}T $)')
        plt.ylabel('Probability density')
        plt.title(
            f'The PDFs of the free energy differences (block size: {b_sizes[0]} ps)')
        plt.grid()
        # save the Guassian plot
        plt.savefig(
            f'free_energy_distribution_all_reps_truncate_{truncate}_avg_{avg_frac}.png', dpi=600)

        df_str = [f"{i: .6f}" for i in df_list]
        df_err_str = [f"{i: .6f}" for i in df_err_list]
        clear_directory(
            f'fes_all_reps_results_truncate_{truncate}_bsize_{b_sizes[0]}_avg_{avg_frac}.dat')
        L = Logging(
            f'fes_all_reps_results_truncate_{truncate}_bsize_{b_sizes[0]}_avg_{avg_frac}.dat')
        result_str = 'Statistics based on all the repetitions:'

        L.logger(result_str)
        L.logger('=' * len(result_str))
        L.logger(f'- Average free energy difference: {np.mean(df_list): .6f}')
        L.logger(f'- Standard deviation of the centers of all reps: {np.std(df_list): .6f}')
        if args.ref is None:
            args.ref = np.mean(df_list)
        rmse = np.sqrt(np.mean((np.array(df_list) - args.ref) ** 2))
        Z = (np.array(df_list) - args.ref) / np.array(df_err_list)
        Z_rms = np.sqrt(np.average(Z ** 2))
        L.logger(f'- Reference value: {args.ref: .6f}')
        L.logger(f'- RMSE value: {rmse: .6f}')
        L.logger(f'- Root-mean-squared Z-score: {Z_rms: .6f}')
        L.logger(f'- The free energy difference of all reps: {df_str}')
        L.logger(
            f'- The uncertainty of free energy difference of all reps: {df_err_str}')

        sec_str = '\nSection 3: Information about the analysis process'
        L.logger(sec_str)
        L.logger('=' * len(sec_str))

        # tabulate and document the maximum memory usage
        table_data = []
        table_data.append(['block_bootstrap', f'{convert_memory_units(max_mem_boot)}'])
        table_data.append(['average_bias', f'{convert_memory_units(max_mem_avg)}'])
        table_data.append(['read_plumed_output', f'{convert_memory_units(max_mem_read)}'])

        x = PrettyTable()
        x.field_names = ['Function name', 'Max memory usage']
        x.add_rows(table_data)
        L.logger(
            f'- The size of the bootstrap array (n_bootstrap, n_block, block_size): ({args.n_bootstraps}, {n_blocks[i]}, {int(n / np.array(n_blocks))})')
        L.logger('- Memory usage')
        L.logger(x)

        T2 = time.time()
        L.logger(f'\nTotal time elapsed: {T2 - T1: .2f} seconds.')


