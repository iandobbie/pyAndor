"""A module for control of Andor cameras via the Andor SDK.

Call from the command line to spawn Pyro objects in separate
processes.
Alternatively, import CameraManager for multiple cameras in one
process.This module defines the Camera class for Andor cameras.

=== Interesting features of the Andor SDK. ===
The DLL relies on runtime-generated handles to access specific
cameras. The handles appear to be generated when there is a call to
GetCameraHandle or Initialize.

The handle for a given camera is not necessarily the same from one
process to the next, which means we need to rely on the camera serial
number. However, it is not possible to sucessfully call
GetCameraSerialNumber until Initialize('') has been completed. Once
Initialize has been called in one process, it grabs the CurrentCamera:
subsequent calls to Initialize in processes that have the same
CurrentCamera will fail, unless the original process made an explicit
call to ShutDown.

So we can't spawn a process then select a known camera:  we have to
blindly pick a different camera for each process and run with what it
gets.

Function names:
* Local function definitions use names as lower_case.
* Direct calls Andor's DLL uses the CapitalCamelCase names it exports.
"""

import andorsdk as sdk
import functools
import numpy
import Pyro4
import re
import sys
import threading
import time
import weakref
from ctypes import byref, c_float, c_int, c_long, c_ulong
from ctypes import create_string_buffer, c_char, c_bool
from multiprocessing import Process, Value, Array
from collections import namedtuple

# A list of the camera models this module supports (or should support.)
SUPPORTED_CAMERAS = ['ixon', 'ixon_plus', 'ixon_ultra']

SETTERS = {
    'exposureTime': ('SetExposureTime', float),
    'EMGain': ('SetEMCCDGain', int),
}

# Amplfier modes are defined by the AD channel, amplifier type,
# and an index into the HSSpeed table.
# I use a list of tuples if label and mode to preserve the label order:
# to preserve this order in an OrderedDict, each mode would have to be
# added individually, making it more difficult both to read or edit.
# Changed this - named tuple's can't work over Pyro.  Doesn't work as
# class AmplifierMode(dict), either.  So just write a function that
# returns the right dict.

def AmplifierMode (label, channel, amplifier, index):
        return {'label': label,
                'channel': channel,
                'amplifier': amplifier,
                'index': index}

AMPLIFIER_MODES = {
    sdk.AC_CAMERATYPE_IXONULTRA: [
        # Verified against the hardware.
        AmplifierMode('EM 1MHz'   , 0, 0, 3),
        AmplifierMode('EM 5MHz'   , 0, 0, 2),
        AmplifierMode('EM 10MHz'  , 0, 0, 1),
        AmplifierMode('EM 17MHz'  , 0, 0, 0),
        AmplifierMode('Conv 80kHz', 0, 1, 2),
        AmplifierMode('Conv 1MHz' , 0, 1, 1),
        AmplifierMode('Conv 3MHz' , 0, 1, 0),
        ],
    sdk.AC_CAMERATYPE_IXON: [
        # Needs to be verified.
        AmplifierMode('EM16 1MHz'   , 1, 0, 0),
        AmplifierMode('EM14 3MHz'   , 0, 0, 0),
        AmplifierMode('EM14 5MHz'   , 0, 0, 1),
        AmplifierMode('EM14 10MHz'  , 0, 0, 2),
        AmplifierMode('Conv16 1MHz' , 1, 1, 0),
        AmplifierMode('Conv16 3MHz' , 0, 1, 0),
        ]
    }

## A lock to prevent concurrent calls to the DLL by different Cameras.
dll_lock = threading.Lock()

def with_camera(func):
    """A decorator for camera functions.

    If there are multiple cameras per process, this decorator obtains a
    lock on the DLL and calls SetCurrentCamera to ensure that the
    library acts on the correct piece of hardware.
    """
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self.singleton:
            # There may be > 1 cameras per process, so lock the DLL.
            had_lock_on_entry = self.has_lock
            if not had_lock_on_entry:
                dll_lock.acquire()
                self.has_lock = True
            try:
                sdk.SetCurrentCamera(self.handle)
                result = func(self, *args, **kwargs)
            except:
                if dll_lock.locked():
                    dll_lock.release()
                    self.has_lock = False
                raise
            if dll_lock.locked() and not had_lock_on_entry:
                dll_lock.release()
                self.has_lock = False

        else:
            # There is only 1 camera per process - no locks required.
            result = func(self, *args, **kwargs)

        return result
    return wrapper


def sdk_call(func):
    """A decorator for DLL functions called as methods of Camera.

    This decorator simply passes args to func, without self.
    """
    @functools.wraps(func)
    def sdk_wrapper(self, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except:
            raise
    return sdk_wrapper


class CameraMeta(type):
    """A metaclass that adds DLL methods to the Camera class.

    Methods from the SDK DLL are wrapped by 'sdk_call' to remove 'self'
    from the args, then by 'with_camera' so that
    * a lock is obtained on the DLL;
    * the DLL is set to act on the Camera instance;
    * the DLL method is called;
    * the lock is released.
    """
    def __new__(meta, classname, supers, classdict):
        for f in sdk.camerafuncs:
            classdict[f.__name__] = with_camera(sdk_call(f))
        return type.__new__(meta, classname, supers, classdict)


class Camera(object):
    """Camera class for Andor cameras.

    All instance methods should be decorated with @with_camera: this
    obtains a lock on the DLL, and sets the target camera for DLL
    methods.
    """
    # Use the CameraMeta class to add DLL methods to this class.
    __metaclass__ = CameraMeta


    def __enter__(self):
        """Context-manager entry."""
        return self


    @with_camera
    def __exit__(self, type, value, traceback):
        """Context-manager exit - call ShutDown to free camera."""
        ## Shut down the camera.
        print "Shutting down camera with handle %s." % self.handle
        try:
            self.ShutDown()
        except:
            # May raise exception if camera never initialized - not a problem.
            pass
        self.enabled = False
        self.armed = False
        self.ready = False


    def __init__(self, handle, singleton=False):
        """Init a Camera instance for hardware with ID=handle."""
        # Number of exposures fetched since last armed.
        self.count = 0
        # SDK's handle for the camera
        self.handle = handle
        # Does this camera have the lock on the DLL?
        self.has_lock = False
        # Detector dimensions in pixels.
        self.nx, self.ny = None, None
        # Detector capabilties.
        self.caps = sdk.AndorCapabilities()
        # Is this the only camera in this process?
        self.singleton = singleton
        # Is the camera enabled?
        self.enabled = False
        # Is the camera ready?
        self.ready = False
        # Is the camera armed to respond to triggers?
        self.armed = False
        # The current acquisition mode.
        self.acquistion_mode = None
        # The current amplifier mode.
        self.amplifier_mode = None
        # Temperature min. and max. possible values.
        self.t_min = c_int()
        self.t_max = c_int()
        # Temperature set point and threshold.
        self.temperature_set_point = c_int()
        self.temperature_state = None
        # Thread to handle data on exposure
        self.data_thread = None
        self.settings = {}


    ### Client functions. ###
    @with_camera
    def abort(self):
        self.AbortAcquisition()
        self.ready = False


    @with_camera
    def arm(self):
        if not self.is_ready():
            raise Exception("Camera not ready.")

        # Open the shutter.
        # SetShutter(type, mode, t_close_ms, t_open_ms)
        # type = 0: TTL high = open; 1: TTL low = open
        # mode = 0: auto, 1: open; 2: closed
        self.SetShutter(1, 1, 1, 1)

        # Reset image count and set armed.
        self.count = 0
        self.armed = True

        # Set camera to espond to triggers.
        self.StartAcquisition()


    @with_camera
    def disable(self):
        self.enabled = False
        try:
            self.abort()
        except:
            pass
        self.make_safe()
        # Could call self.Shutdown here, but that would require slow
        # AD calibration on next enable.
        # TODO: figure out where to call ShutDown before exit / on
        # camera destruction ... can't see how to use a 'with' context
        # manager here.


    @with_camera
    def enable(self, settings):
        # Clear the flag so that our client will poll until it is True.
        self.enabled = False
        try:
            # Stop whatever the camera was doing.
            self.abort()
        except Exception:
            try:
                self.Initialize('')
            except:
                raise

        # Get detector size and capabilities.
        self.get_detector()
        self.get_capabilities()

        # Enable temperature control.
        if settings.get('isWaterCooled'):
            # Use fan at low speed.
            self.SetFanMode(1)
        else:
            # Use fan at full speed.
            self.SetFanMode(0)
        self.GetTemperatureRange(self.t_min, self.t_max)

        # If a temperature is specified, then use it. Otherwise, hardware
        # default or previos value will be used.
        target_t = settings.get('targetTemperature')
        if target_t is not None:
            # Temperature set-point is limited to available range.
            self.temperature_set_point.value = max(
                                        self.t_min.value,
                                        min(self.t_max.value, int(target_t)))
            self.SetTemperature(self.temperature_set_point)

        # Turn the cooler on.
        self.CoolerON()

        # Do not assume anything about the camera state: set everything.
        self.update_settings(settings, init=True)


        # Set the acquisition mode to single frame.
        self.set_acquisition_mode(1)

        # Enable external triggering.
        self.SetTriggerMode(1)

        # Recalculate VS speed.
        self.set_fastest_vs_speed()

        # Set enabled indicator flag.
        self.enabled = True
        return self.enabled


    def get_image_size(self):
        return (self.nx, self.ny)


    @with_camera
    def get_exposure_time(self):
        (exposure, accumulate, kinetics) = self.get_acquisition_timings()
        if self.acquisition_mode in [1, 5]:
            # single exposure or run untul abort
            return exposure
        elif self.acquisition_mode == 2:
            # accumulate mode
            return accumulate
        elif self.acquisition_mode in [3, 4]:
            # kinetics or fast kinetics
            return kinetics


    @with_camera
    def get_min_time_between_exposures(self):
        (exposure, accumulate, kinetics) = self.get_acquisition_timings()
        if self.acquisition_mode in [1, 5]:
            # single exposure or run until abort
            return self.get_read_out_time()
        elif self.acquisition_mode == 2:
            # accumulate mode
            return accumulate - exposure
        elif self.acquisition_mode in [3, 4]:
            # kinetics mode
            return kinetics - exposure


    @with_camera
    def make_safe(self):
        """ Put the camera into a safe but active state."""
        # Set lowest EM gain on EMCCD cameras.
        if self.caps.ulSetFunctions & sdk.AC_SETFUNCTION_EMCCDGAIN:
            self.SetEMCCDGain(0)

        # Switch to conventional amplifier on supported cameras.
        if self.caps.ulCameraType in [sdk.AC_CAMERATYPE_CLARA,
                                      sdk.AC_CAMERATYPE_IXON,
                                      sdk.AC_CAMERATYPE_IXONULTRA,
                                      sdk.AC_CAMERATYPE_NEWTON]:
            self.SetOutputAmplifier(1)




    @with_camera
    def prepare(self, experiment):
        """Prepare the camera for data acquisition."""
        self.get_detector()
        self.get_capabilities()


    def receiveClient(self, uri):
        """Handle connection request from cockpit client."""
        if uri is None:
            self.client = None
        else:
            self.client = Pyro4.Proxy(uri)
            if self.data_thread is not None:
                self.data_thread.set_client(self.client)


    @with_camera
    def update_settings(self, settings, init=False):
        # Clear the flag so that our client will poll until it is True.
        self.enabled = False
        try:
            # Stop whatever the camera was doing.
            self.abort()
        except Exception:
            try:
                self.Initialize('')
            except:
                raise

        if init:
            # Assume nothing about state: set everything.
            # Update our settings with incoming settings.
            update_keys = set(self.settings.keys())
        else:
            # Only update new and changed values.
            my_keys = set(self.settings.keys())
            their_keys = set(settings.keys())
            update_keys = their_keys.union(
                            set(key for key in
                               my_keys.intersection(their_keys)
                               if self.settings[key] != settings[key]))


        # Update this cameras settings dict.
        self.settings.update(settings)
        for key in update_keys:
            try:
                funcstr, argtype = SETTERS.get(key)
            except:
                # raise Exception("No setter specified for '%s'." % key)
                pass
                # There are some settings we don't handle.
            else:
                func = getattr(self, funcstr)
                func(argtype(self.settings[key]))

        # Recalculate and apply fastest vertical shift speed.
        self.set_fastest_vs_speed()

        # Set enabled indicator flag.
        self.enabled = True
        return self.enabled



    ### (Fairly) simple wrappers and utility functions. ###
    @with_camera
    def get_acquisition_timings(self):
        exposure = c_float()
        accumulate = c_float()
        kinetic = c_float()
        sdk.GetAcquisitionTimings(exposure, accumulate, kinetic)
        return (exposure.value, accumulate.value, kinetic.value)


    @with_camera
    def get_amp_desc(self, index):
        s = create_string_buffer(128)
        sdk.GetAmpDesc(index, s, len(s))
        return s.value


    @with_camera
    def get_amplifier_modes(self):
        if not self.caps.ulCameraType:
            self.get_capabilities()
        # Return the amplifier mode labels.
        modes = AMPLIFIER_MODES[self.caps.ulCameraType]
        
        return modes


    @with_camera
    def get_camera_serial_number(self):
        sn = c_int()
        sdk.GetCameraSerialNumber(sn)
        return sn.value


    @with_camera
    def get_capabilities(self):
        sdk.GetCapabilities(self.caps)
        return self.caps


    @with_camera
    def get_detector(self):
        """Populate nx and ny with the detector geometry."""
        nx, ny = c_int(), c_int()
        sdk.GetDetector(byref(nx), byref(ny))
        self.nx = nx.value
        self.ny = ny.value
        return (self.nx, self.ny)


    @with_camera
    def get_em_advanced(self):
        state = c_int()
        sdk.GetEMAdvanced(state)
        return state.value


    @with_camera
    def get_emccd_gain(self):
        gain = c_int()
        sdk.GetEMCCDGain(gain)
        return gain.value


    @with_camera
    def get_em_gain_range(self):
        low = c_int()
        high = c_int()
        sdk.GetEMGainRange(low, high)
        return (low.value, high.value)


    @with_camera
    def get_fk_exposure_time(self):
        t = c_float()
        sdk.GetFKExposureTime(t)
        return t.value


    @with_camera
    def get_fk_v_shift_speed_f(self, index):
        speed = c_float()
        sdk.GetFKVShiftSpeedF(index, speed)
        return speed.value


    @with_camera
    def get_fastest_recommended_vs_speed(self):
        index = c_int()
        speed = c_float()
        sdk.GetFastestRecommendedVSSpeed(index, speed)
        self.vs_speed = speed.value
        return (index.value, speed.value)


    @with_camera
    def get_read_out_time(self):
        t = c_float()
        self.GetReadOutTime(t)
        return t.value


    @with_camera
    def get_temperature(self):
        temperature = c_int()
        self.temperature_state = sdk.GetTemperature(temperature)
        return temperature.value


    @with_camera
    def get_hardware_version(self):
        pcb, decode, dummy1, dummy2, version, build = 6 * [c_ulong()]
        plist = [pcb, decode, dummy1, dummy2, version, build]
        parameters = [byref(p) for p in plist]
        sdk.GetHardwareVersion(*parameters)
        result = [p.value for p in plist]
        return result


    @with_camera
    def get_head_model(self):
        s = create_string_buffer(128)
        sdk.GetHeadModel(s)
        return s.value


    @with_camera
    def is_preamp_gain_available(self, channel, amplifier, index, gain):
        status = c_int()
        sdk.IsPreAmpGainAvailable(int(channel),
                                  int(amplifier),
                                  int(index),
                                  int(gain),
                                  status)
        return status.value


    @with_camera
    def is_ready(self):
        status = self.GetTemperature(c_int())
        self.ready = (status == sdk.DRV_TEMP_STABILIZED) and self.enabled
        return self.ready


    @with_camera
    def set_amplifier_mode(self, mode):
        result = []
        result.append(self.SetADChannel(mode.get('channel')))
        result.append(self.SetOutputAmplifier(mode.get('amplifier')))
        result.append(self.SetHSSpeed(mode.get('amplifier'), mode.get('speed')))


    @with_camera
    def set_acquisition_mode(self, mode):
        self.SetAcquisitionMode(mode)
        self.acquisition_mode = mode


    @with_camera
    def set_em_gain(self, value):
        return self.SetEMCCDGain(value)


    @with_camera
    def set_exposure_time(self, exposure_time):
        """Set the exposure time and update vertical shift speed."""
        self.SetExposureTime(float(exposure_time))
        self.set_fastest_vs_speed()
        exposure, accumulate, kinetic = self.get_acquisition_timings()
        return exposure

    @with_camera
    def set_fastest_vs_speed(self):
        """Update the vertical shift speed to fasted recommended speed."""
        (index, speed) = self.get_fastest_recommended_vs_speed()
        self.SetVSSpeed(int(index))
        return speed


class DataThread(threading.Thread):
    """A thread to collect acquired data and dispatch it to a client."""
    def __init__(self, cam):
        threading.Thread.__init__(self)
        self.skip_next_n_images = 0
        self.exposure_count = 0
        self.skip_every_n_images = 1
        self.should_quit = False
        self.cam = weakref.proxy(cam)
        self.image_array = numpy.zeros((cam.nx, cam.ny), dtype=numpy.uint16)
        self.n_pixels = cam.nx * cam.ny


    def __del__(self):
        if self.is_alive:
            self.should_quit = True


    def run(self):
        while not self.should_quit:
            try:
                result = self.cam.GetOldestImage16(self.image_array,
                                                   self.n_pixels)
            except:
                raise

            if result == sdk.DRV_SUCCESS:
                timestamp = 0 # TODO - fix timestamp.
                cam.count += 1
                if self.client is not None:
                    self.client.receiveData('new image',
                                             self.image_array,
                                             timestamp)
            else:
                time.sleep(0.01)


    def set_client(client):
        self.client = client


class StatusThread(threading.Thread):
    """A thread to maintain a status string."""
    def __init__(self, cam, port, status):
        threading.Thread.__init__(self)
        self.cam = cam
        self.status = status
        self.status.port.value = port
        self.should_quit = False


    def __del__(self):
        if self.is_alive:
            self.should_quit = True


    def run(self):
        while not self.should_quit:
            if not self.status.live.value:
            # Status does not show camera is live.
                serial = None
                try:
                    serial = self.cam.get_camera_serial_number()
                except:
                    # Driver probably not initialized.
                    pass
                # Did we get a serial number?
                if serial:
                    self.status.serial.value = serial
                    self.status.live.value = True
            else:
            # Status shows camera is live.
                try:
                    temperature = self.cam.get_temperature()
                except:
                    # Driver probably finished initialization.
                    self.status.valid_temperature.value = False
                else:
                    self.status.valid_temperature.value = True
                    self.status.temperature.value = temperature

                self.status.enabled.value = self.cam.enabled
                self.status.ready.value = self.cam.ready
                self.status.armed.value = self.cam.armed
                self.status.count.value = self.cam.count

                self.status.EMGain.value = self.cam.settings.get('EMGain', 0)

            time.sleep(1)


class StatusObject(object):
    """StatusObject is used to share camera status between processes."""
    def __init__(self):
        self.pid = Value(c_int)
        self.armed = Value(c_bool)
        self.enabled = Value(c_bool)
        self.count = Value(c_int)
        self.port = Value(c_int)
        self.ready = Value(c_bool)
        self.serial = Value(c_int)
        self.temperature = Value(c_int)
        self.EMGain = Value(c_int)
        # These values are False until we find otherwise.
        self.valid_temperature = Value(c_bool, False)
        self.live = Value(c_bool, False)


class CameraManager(object):
    """A class to manage Camera instances.

    This used to be handled by class variables and class methods, but
    that approach will not work when we need to spread several cameras
    over separate processes.
    """
    def __init__(self):
        # Map handle values to camera instances
        self.handle_to_camera = {}
        # A list of camera instances
        self.cameras = []
        self.num_cameras = c_long()
        sdk.GetAvailableCameras(self.num_cameras)


    def update_cameras(self):
        """Search for cameras and create Camera instances.

        Camera instances are tracked in the class variables
        Camera.cameralist and Camera.handle_to_camera.
        """
        for cam in self.cameras:
            self.cameras.remove(cam)

        num_cameras = c_long()
        sdk.GetAvailableCameras(num_cameras)

        for i in range(num_cameras.value):
            handle = c_long()
            sdk.GetCameraHandle(i, handle)
            self.cameras.append(Camera(handle))
            self.handle_to_camera.update({handle.value: i})


class CameraServer(Process):
    """A class to serve Camera objects over Pyro."""
    def __init__(self, index, status=None):
        super(CameraServer, self).__init__()
        # daemon=True means that this process will be terminated with parent.
        self.daemon = True
        self.index = index
        #TODO - should read serial_to_* from config file.
        self.serial_to_host = {9145: '127.0.0.1', 9146: '127.0.0.1'}
        self.serial_to_port = {9145: 7776, 9146: 7777}
        self.cam = None
        self.status = status


    def run(self):
        self.serve()


    def serve(self):
        ## This works for each camera ... once.  If a process is
        # terminated then you try and create a new process for the same
        # camera, the SDK will not succesfully Initialize.

        # TODO: I think we may need to call Shutdown before the
        # process exits ... but Process.terminate kills it right away,
        # so we probaly need to stop the Pyro Daemon, then call
        # Shutdown, then return / join the main process.
        serial = None
        num_cameras = c_long()
        handle = c_long()

        sdk.GetAvailableCameras(num_cameras)
        if self.index >= num_cameras.value:
            raise Exception(
                "Camera index %d too large: I only have %d cameras."
                % (self.index, num_cameras.value))

        sdk.GetCameraHandle(self.index, handle)

        # SetCurrentCamera once, and create Camera with singleton=True:
        # this is the only camera in this process, so we don't need to
        # SetCurrentCamera and lock for each Camera method.
        sdk.SetCurrentCamera(handle)

        self.cam = Camera(handle, singleton=True)
        self.cam.Initialize('')
        serial = self.cam.get_camera_serial_number()

        if not self.serial_to_host.has_key(serial):
            raise Exception("No host found for camera with serial number %s."
                                % serial)

        if not self.serial_to_port.has_key(serial):
            raise Exception("No host found for camera with serial number %s."
                                % serial)

        host = self.serial_to_host[serial]
        port = self.serial_to_port[serial]

        if self.status:
            self.status.pid = self.pid
            status_thread = StatusThread(self.cam, port, self.status)
            status_thread.start()

        daemon = Pyro4.Daemon(port=port, host=host)
        Pyro4.Daemon.serveSimple({self.cam: 'pyroCam'},
                                 daemon=daemon, ns=False, verbose=True)



def report_status(status_list):
    labels = ('ID', 'PID', 'port', 'enab', 'ready', 
              'armed', 'temp', 'count', 'gain')
    
    format_str = '|'.join(['%%%ds' % (len(label) + 2) for label in labels])
    format_str += '\n'

    # Would be nice to save and restore the cursor position, but there
    # is no stock curses support under Windows.
    # Move cursor to top of console.
    sys.stdout.write('\033[1;1H')

    # Generate and display a header row.
    sstr = format_str % labels
    sys.stdout.write(sstr)

    # Generate and display a status row for each status.
    for i, status in enumerate(statuses):
        sstr = ''
        sstr += '\033[30m' #and Black foreground
        if not status.live.value:
            sstr += '\033[41m' # red background
        elif status.live.value and not status.enabled.value:
            sstr += '\033[43m' # yellow background
        elif status.live.value and status.enabled.value:
            sstr += '\033[42m' # green background
        else:
            sstr += '\033[47m' # white background
        sstr += format_str % (
            status.serial.value,
            status.pid.value,
            status.port.value,
            status.enabled.value,
            status.ready.value,
            status.armed.value,
            status.temperature.value if status.valid_temperature.value else '???',
            status.count.value,
            status.EMGain.value,)
        sys.stdout.write(sstr)
    # Reset colours.
    sys.stdout.write('\033[39m\033[49m')
    # Move cursor down a few rows.
    sys.stdout.write('\033[%d;1H' % (num_cameras.value + 4))
    time.sleep(0.5)

if __name__ == '__main__':
    """ If called from the command line, create CameraServers.

    There will be a delay while the dll initializes each camera:  it
    does not seem able to do concurrent initializations, even in
    separate processes.  Hopefully, this will prevent any delay each
    time we connect to the camera from cockpit.
    """
    # Fix for a win32 and colorama bug
    import colorama
    try:
        test = colorama.Win32.COORD(0,0)
    except:
        from ctypes.wintypes import _COORD
        colorama.win32.COORD = _COORD
    else:
        del(test)
    colorama.init()

    num_cameras = c_long()
    sdk.GetAvailableCameras(num_cameras)

    children = []
    statuses = []
    sys.stdout.write('\033[2J') # Clear the terminal window.
    sys.stdout.write("Found %d cameras." % num_cameras.value)

    for i in range(num_cameras.value):
        # For each camera we found ...
        # ... create a shared status object, ...
        statuses.append(StatusObject())
        # ... spawn a CameraServer in a separate process, ...
        children.append(CameraServer(i, status=statuses[i]))
        sys.stdout.write("Starting service %d of %d in daemon process ..."
                % (i + 1, num_cameras.value))
        # ... and start the child process.
        children[i].start()

    try:
        while True:
            if statuses:
                report_status(statuses)
            else:
                # nothing to do.
                pass
    finally:
        for c in children:
            # As this process exits, clean up child processes.
            c.terminate()
