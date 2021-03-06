'''

'''
import os
import sys
import numpy as np
import math
import time
from bspyproc.processors.hardware import task_mgr
from bspyproc.processors.hardware.brains_setup_list import BrainsDevices as Bsetup
from bspyproc.utils.control import get_control_voltage_indices, merge_inputs_and_control_voltages_in_numpy
import nidaqmx.system.device as device
import signal
import threading
from threading import Thread
import queue


# SECURITY FLAGS.
# WARNING - INCORRECT VALUES FOR THESE FLAGS CAN RESULT IN DAMAGING THE DEVICES
INPUT_VOLTAGE_THRESHOLD = 1.5
CDAQ_TO_NIDAQ_RAMPING_TIME_SECONDS = 0.1
CDAQ_TO_CDAQ_RAMPING_TIME_SECONDS = 0.03
SYNCHRONISATION_VALUE = 0.04  # do not reduce to less than 0.02


class NationalInstrumentsSetup():

    def __init__(self, configs):
        self.enable_os_signals()
        self.configs = configs
        if configs['max_ramping_time_seconds'] == 0:
            input("WARNING: IF YOU PROCEED THE DEVICE CAN BE DAMAGED. READ THIS MESSAGE CAREFULLY. \n The security check for the ramping time has been disabled. Steep rampings can can damage the device. Proceed only if you are sure that you will not damage the device. If you want to avoid damagesimply exit the execution. \n ONLY If you are sure about what you are doing press ENTER to continue. Otherwise STOP the execution of this program.")
        assert configs['waveform']['slope_lengths'] / configs['sampling_frequency'] >= configs['max_ramping_time_seconds']
        # self.input_indices = configs['input_indices']
        # self.control_voltage_indices = get_control_voltage_indices(self.input_indices, configs['input_electrode_no'])
        self.driver = task_mgr.get_driver(configs['driver'])
        self.offsetted_shape = configs['shape'] + configs['offset']
        self.ceil = math.ceil((self.offsetted_shape) / self.configs['sampling_frequency']) + 1
        self.driver.init_output(configs['input_channels'], self.configs['sampling_frequency'], self.offsetted_shape)
        time.sleep(1)
        self.driver.init_input(configs['output_channels'], self.configs['sampling_frequency'], self.offsetted_shape)
        global event
        global semaphore
        event = threading.Event()
        semaphore = threading.Semaphore()
        # self.results_queue = queue.Queue()

    def reset(self):
        self.close_tasks()
        device.Device(name=self.configs['input_instrument']).reset_device()
        device.Device(name=self.configs['output_instrument']).reset_device()

    def process_output_data(self, data):
        data = np.asarray(data)
        if len(data.shape) == 1:
            data = data[np.newaxis, :]
        return data * self.configs["amplification"]

    def read_data(self, y):
        global p
        # p = Thread(target=lambda q, arg1: q.put(self._read_data(arg1)), args=(self.results_queue, y))
        p = Thread(target=self._read_data, args=(y,))
        if not event.is_set():
            semaphore.acquire()
            p = Thread(target=self._read_data, args=(y,))
            p.start()
            p.join()
            if self.data_results is None:
                print('Nothing could be read. Stopping program')
                self.os_signal_handler(None)
            semaphore.release()
        return self.data_results
        # results = self.results_queue.get()
        # self.results_queue.task_done()
        # return

    def _read_data(self, y):
        '''
            y = It represents the input data as matrix where the shpe is defined by the "number of inputs to the device" times "input points that you want to input to the device".
        '''
        self.data_results = None
        self.read_security_checks(y)
        self.driver.start_tasks(y, self.configs['auto_start'])
        read_data = self.driver.read(self.offsetted_shape, self.ceil)
        self.driver.stop_tasks()
        self.data_results = read_data
        return read_data

    def read_security_checks(self, y):
        for n, y_i in enumerate(y):
            assert all(y_i < INPUT_VOLTAGE_THRESHOLD), f"Voltages in electrode {n} higher ({y_i.max()}) than the max. allowed value ({INPUT_VOLTAGE_THRESHOLD} V)"
            assert all(y_i > -INPUT_VOLTAGE_THRESHOLD), f"Voltages in electrode {n} lower ({y_i.min()}) than the min. allowed value ({-INPUT_VOLTAGE_THRESHOLD} V)"
            assert y_i[0] == 0., f"First value of input stream in electrode {n} is non-zero ({y_i[0]})"
            assert y_i[-1] == 0., f"Last value of input stream in electrode {n} is non-zero ({y_i[-1]})"

    def close_tasks(self):
        self.driver.close_tasks()

    def get_amplification_value(self):
        return self.configs["amplification"]

    # def get_output_(self, inputs, control_voltages):
    #     y = merge_inputs_and_control_voltages_in_numpy(inputs, control_voltages, self.input_indices, self.control_voltage_indices)
    #     return self.get_output(y)s

    def get_output(self):
        pass

    # These functions are used to handle the termination of the read task in such a way that enables the last read to finish, and closes the tasks afterwards

    def os_signal_handler(self, signum, frame=None):
        event.set()
        print('Interruption/Termination signal received. Waiting for the reader to finish.')
        p.join()
        # print('Emptying the results queue')
        # if not self.results_queue.empty():
        #     self.results_queue.get()
        # print('Waiting for the queue to finish')
        # self.results_queue.join()
        print("Closing nidaqmx tasks")
        self.close_tasks()
        sys.exit(0)

    def enable_os_signals(self):
        if sys.platform == "win32":
            import win32api
            win32api.SetConsoleCtrlHandler(self.os_signal_handler, True)
        else:
            signal.signal(signal.SIGTERM, self.os_signal_handler)
            signal.signal(signal.SIGINT, self.os_signal_handler)

    def disable_os_signals(self):
        if sys.platform == "win32":
            import win32api  # ignoring the signal
            win32api.SetConsoleCtrlHandler(None, True)
        else:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            

class BrainsCDAQtoCDAQ(NationalInstrumentsSetup):

    def __init__(self, configs):
    
        configs['auto_start'] = True
        configs['offset'] = 1
        configs['max_ramping_time_seconds'] = CDAQ_TO_CDAQ_RAMPING_TIME_SECONDS
        BrainsCDAQtoCDAQ.get_brains_channels(self, configs)
        configs['input_channels'] = BrainsCDAQtoCDAQ.create_ao_channels(self, configs)
        configs['output_channels'] = BrainsCDAQtoCDAQ.create_ai_channels(self, configs)
        super().__init__(configs)
        self.driver.start_trigger(self.configs['trigger_source'])
        
    def get_brains_channels(self, configs):
        
        devlist = Bsetup.setup_devices_brains()
        mask = configs['output_channel_mask']
        self.out_ch_list = []
        self.in_ch_list = []
        for dev in configs['devices']:
            dev_tmp = devlist[dev]
            self.in_ch_list.append(dev_tmp["input_channel"])
            for j in range(7):
                if mask[dev][j] == 1:
                    self.out_ch_list.append(dev_tmp["output_channels"][j])
           
    def create_ao_channels(self, configs):
        ao_channels = []
        if configs['processor_type'] == 'cdaq_to_cdaq':
            for i in range(len(configs['input_channels'])):
                ao_channels.append(configs['output_instrument'] + "/ao" + str(configs['input_channels'][i]))
        elif configs['processor_type'] == 'brains_cdaq_to_cdaq'::          
            ao_channels = self.out_ch_list
        else :
            print('error in config(architecture), select single_device or multi_device')
        return ao_channels
        
    def create_ai_channels(self, configs):
        ai_channels = []
        if configs['processor_type'] == 'cdaq_to_cdaq':
            for i in range(len(configs['output_channels'])):
                ai_channels.append(configs['input_instrument'] + "/ai" + str(configs['output_channels'][i]))
        elif configs['processor_type'] == 'brains_cdaq_to_cdaq':
            ai_channels = self.in_ch_list
        else :
            print('error in config(architecture), select single_device or multi_device')
        return ai_channels
        
    def get_output(self, y):
        # y = np.concatenate((y, y[-1, :] * np.ones((1, y.shape[1]))))
        y = y.T
        # assert self.configs['shape'] + 1 == y.shape[1], f"configs value with key 'shape' must be {y.shape[1]-1}"
        data = self.read_data(y)
        data = -1 * self.process_output_data(data)[:, 1:]
        return data.T
        
class CDAQtoCDAQ(NationalInstrumentsSetup):

    def __init__(self, configs):
        configs['auto_start'] = True
        configs['offset'] = 1
        configs['max_ramping_time_seconds'] = CDAQ_TO_CDAQ_RAMPING_TIME_SECONDS
        configs['input_channels'] = BrainsCDAQtoCDAQ.create_ao_channels(self, configs)
        configs['output_channels'] = BrainsCDAQtoCDAQ.create_ai_channels(self, configs)
        super().__init__(configs)
        self.driver.start_trigger(self.configs['trigger_source'])

    def get_output(self, y):
        # y = np.concatenate((y, y[-1, :] * np.ones((1, y.shape[1]))))
        y = y.T
        # assert self.configs['shape'] + 1 == y.shape[1], f"configs value with key 'shape' must be {y.shape[1]-1}"
        data = self.read_data(y)
        data = -1 * self.process_output_data(data)[:, 1:]
        return data.T


class CDAQtoNiDAQ(NationalInstrumentsSetup):

    def __init__(self, configs):
        configs['auto_start'] = False
        configs['offset'] = int(configs['sampling_frequency'] * SYNCHRONISATION_VALUE)
        configs['max_ramping_time_seconds'] = CDAQ_TO_NIDAQ_RAMPING_TIME_SECONDS
        super().__init__(configs)
        self.driver.add_channels(self.configs['output_instrument'], self.configs['input_instrument'])

    def get_output(self, y):
        y = y.T
        assert self.configs['shape'] == y.shape[1], f"configs value with key 'shape' must be {y.shape[1]}"
        y = self.synchronise_input_data(y)
        max_attempts = 5
        attempts = 1
        finished = False
        while not finished and (attempts < max_attempts):
            data, finished = self.readout_trial(y)
            attempts += 1

        assert finished, ('Error: output data not same size as input data. Output: ' +
                          str(data.shape[1]) + ' points, input: ' + str(self.configs['shape']) + ' points.')
        return data.T

    def readout_trial(self, y):
        data = self.read_data(y)
        data = self.process_output_data(data)
        data = self.synchronise_output_data(data)
        finished = data.shape[1] == self.configs['shape']
        return data, finished

    def synchronise_input_data(self, y):
        # TODO: Are the following three lines really necessary?
        y = np.asarray(y)
        if len(y.shape) == 1:
            y = y[np.newaxis, :]
        # Append some zeros to the initial signal such that no input data is lost
        # This should be handled with proper synchronization
        y_corr = np.zeros((y.shape[0], y.shape[1] + self.configs['offset']))  # Add 200ms of reaction in terms of zeros
        y_corr[:, self.configs['offset']:] = y[:]
        # TODO: Is this if really necessary?
        if len(y_corr.shape) == 1:
            y_corr = np.concatenate((y_corr[np.newaxis], np.zeros((1, y_corr.shape[1]))))   # Set the trigger
        else:
            y_corr = np.concatenate((y_corr, np.zeros((1, y_corr.shape[1]))))   # Set the trigger
        y_corr[-1, self.configs['offset']] = 1  # Start input data

        return y_corr

    def get_output_cut_value(self, read_data):
        cut_value = np.argmax(read_data[-1, :])
        if read_data[-1, cut_value] < 0.05:
            print('Warning: initialize spike not recognized')
        return cut_value

    def synchronise_output_data(self, read_data):
        cut_value = self.get_output_cut_value(read_data)
        return read_data[:-1, cut_value:self.configs['shape'] + cut_value]
