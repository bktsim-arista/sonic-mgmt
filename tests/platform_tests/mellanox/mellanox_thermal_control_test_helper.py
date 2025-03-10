import os
import json
import random
import logging
import time
from pkg_resources import parse_version
from tests.platform_tests.thermal_control_test_helper import mocker, FanStatusMocker, ThermalStatusMocker, \
    SingleFanMocker
from tests.common.mellanox_data import get_hw_management_version, get_platform_data
from .minimum_table import get_min_table
from tests.common.utilities import wait_until
from tests.common.helpers.assertions import pytest_assert


NOT_AVAILABLE = 'N/A'

THERMAL_NAMING_RULE = {
    "cpu_core": {
        "name": "CPU Core {} Temp",
        "temperature": "cpu_core{}",
        "high_threshold": "cpu_core{}_max",
        "high_critical_threshold": "cpu_core{}_crit"
    },
    "module": {
        "name": "xSFP module {} Temp",
        "temperature": "module{}_temp_input",
        "high_threshold": "module{}_temp_crit",
        "high_critical_threshold": "module{}_temp_emergency"
    },
    "psu": {
        "name": "PSU-{} Temp",
        "temperature": "psu{}_temp1",
        "high_threshold": "psu{}_temp1_max"
    },
    "cpu_pack": {
        "name": "CPU Pack Temp",
        "temperature": "cpu_pack",
        "high_threshold": "cpu_pack_max",
        "high_critical_threshold": "cpu_pack_crit"
    },
    "gearbox": {
        "name": "Gearbox {} Temp",
        "temperature": "gearbox{}_temp_input",
        "high_threshold": "gearbox{}_temp_emergency",
        "high_critical_threshold": "gearbox{}_temp_trip_crit"
    },
    "asic_ambient": {
        "name": "ASIC",
        "temperature": "asic",
        "high_threshold": "asic_temp_emergency",
        "high_critical_threshold": "asic_temp_trip_crit"
    },
    "port_ambient": {
        "name": "Ambient Port Side Temp",
        "temperature": "port_amb"
    },
    "fan_ambient": {
        "name": "Ambient Fan Side Temp",
        "temperature": "fan_amb"
    },
    "comex_ambient": {
        "name": "Ambient COMEX Temp",
        "temperature": "comex_amb"
    },
    "cpu_ambient": {
        "name": "Ambient CPU Board Temp",
        "temperature": "cpu_amb"
    },
    "pch": {
        "name": "PCH Temp",
        "temperature": "pch_temp"
    },
    "sodimm": {
        "name": "SODIMM {} Temp",
        "temperature": "sodimm{}_temp_input",
        "high_threshold": "sodimm{}_temp_max",
        "high_critical_threshold": "sodimm{}_temp_crit"
    }
}

thermal_rule_patched = False

THERMAL_RULE_PATCHES = {
    "201911": {
        "asic_ambient": {
            "name": "Ambient ASIC Temp",
            "temperature": "asic"
        },
        "gearbox": {
            "name": "Gearbox {} Temp",
            "temperature": "gearbox{}_temp_input"
        }
    },
    "hw-mgmt.7.0030.2003.before": {
        "asic_ambient": {
            "name": "ASIC",
            "temperature": "asic",
            "high_threshold": "mlxsw/temp_trip_hot",
            "high_critical_threshold": "mlxsw/temp_trip_crit"
        },
        "gearbox": {
            "name": "Gearbox {} Temp",
            "temperature": "gearbox{}_temp_input",
            "high_threshold": "mlxsw-gearbox{}/temp_trip_hot",
            "high_critical_threshold": "mlxsw-gearbox{}/temp_trip_crit"
        },
        "psu": {
            "name": "PSU-{} Temp",
            "temperature": "psu{}_temp",
            "high_threshold": "psu{}_temp_max"
        }
    }
}


def patch_thermal_rule(mock_helper):
    """
    Patch thermal rule for different sonic version/kernel version/hw-management version.
    This function is mainly for backward compatible.
    :param mock_helper: Mock helper.
    """
    global thermal_rule_patched
    global THERMAL_NAMING_RULE

    if thermal_rule_patched:
        return

    patch = None
    if mock_helper.is_201911():
        patch = THERMAL_RULE_PATCHES['201911']
    else:
        hw_mgmt_version = get_hw_management_version(mock_helper.dut)
        if parse_version(hw_mgmt_version) < parse_version('7.0030.2003'):
            patch = THERMAL_RULE_PATCHES['hw-mgmt.7.0030.2003.before']

    if patch is not None:
        for key, rule in patch.items():
            THERMAL_NAMING_RULE[key] = rule

    thermal_rule_patched = True


FAN_NAMING_RULE = {
    "fan": {
        "name": "fan{}",
        "speed": "fan{}_speed_get",
        "presence": "fan{}_status",
        "target_speed": "fan{}_speed_set",
        "max_speed": "fan{}_max",
        "status": "fan{}_fault",
        "led_capability": "led_fan{}_capability",
        "led_green": "led_fan{}_green",
        "led_red": "led_fan{}_red",
        "led_orange": "led_fan{}_orange"
    },
    "psu_fan": {
        "name": "psu{}_fan1",
        "speed": "psu{}_fan1_speed_get",
        "power_status": "psu{}_pwr_status",
        "max_speed": "psu{}_fan_max",
    }
}

SUSPEND_FILE_PATH = "/var/run/hw-management/config/suspend"


class SysfsNotExistError(Exception):
    """
    Exception when sys fs not exist.
    """
    pass


class MockerHelper:
    """
    Mellanox specified mocker helper.
    """

    # Thermal related sys fs folder path.
    THERMAL_PATH = '/var/run/hw-management/thermal/'

    # LED related sys fs folder path.
    LED_PATH = '/var/run/hw-management/led/'

    # Config path
    CONFIG_PATH = '/var/run/hw-management/config/'

    # Power path
    POWER_PATH = '/var/run/hw-management/power/'

    # FAN number of DUT.
    FAN_NUM = 0

    # FAN number for each FAN drawer of DUT.
    FAN_NUM_PER_DRAWER = 0

    # Flag indicate if FAN number has been initialize.
    INIT_FAN_NUM = False

    unlink_file_list = {}
    regular_file_list = {}

    def __init__(self, dut):
        """
        Constructor of mocker helper.
        :param dut: DUT object representing a SONiC switch under test.
        """
        self.dut = dut
        self._extract_num_of_fans_and_fan_drawers()
        self.deinit_retry = 5

    def _extract_num_of_fans_and_fan_drawers(self):
        """
        Get FAN number and Fan number for each FAN drawer of this DUT.
        :return:
        """
        if MockerHelper.INIT_FAN_NUM:
            return

        MockerHelper.INIT_FAN_NUM = True
        get_drawer_num_cmd = 'ls {}fan*_status | wc -l'.format(
            MockerHelper.THERMAL_PATH)
        output = self.dut.shell(get_drawer_num_cmd)
        content = output['stdout'].strip()
        if not content:
            return
        fan_drawer_num = int(content)

        get_fan_num_cmd = 'ls {}fan*_speed_get | wc -l'.format(
            MockerHelper.THERMAL_PATH)
        output = self.dut.shell(get_fan_num_cmd)
        content = output['stdout'].strip()
        if not content:
            return

        MockerHelper.FAN_NUM = int(content)

        if MockerHelper.FAN_NUM > fan_drawer_num:
            MockerHelper.FAN_NUM_PER_DRAWER = 2
        else:
            MockerHelper.FAN_NUM_PER_DRAWER = 1

    def mock_thermal_value(self, file_path, value):
        """
        Mock thermal related value whose sys fs folder is at '/var/run/hw-management/thermal/'
        :param file_path: Sys fs file name.
        :param value: Value to write to sys fs file.
        :return:
        """
        file_path = os.path.join(MockerHelper.THERMAL_PATH, file_path)
        self.mock_value(file_path, value)

    def mock_led_value(self, file_path, value):
        """
        Mock led related value whose sys fs folder is at '/var/run/hw-management/led/'
        :param file_path: Sys fs file name.
        :param value: Value to write to sys fs file.
        :return:
        """
        file_path = os.path.join(MockerHelper.LED_PATH, file_path)
        self.mock_value(file_path, value)

    def mock_value(self, file_path, value, force=False):
        """
        Unlink existing sys fs file and replace it with a new one. Write given value to the new file.
        :param file_path: Sys fs file path.
        :param value: Value to write to sys fs file.
        :param force: Force mock even if the file does not exist.
        :return:
        """
        if file_path not in self.regular_file_list and file_path not in self.unlink_file_list:
            out = self.dut.stat(path=file_path)
            exist = True
            if not out['stat']['exists']:
                if force:
                    exist = False
                else:
                    raise SysfsNotExistError('{} not exist'.format(file_path))
            if exist and out['stat']['islnk']:
                self._unlink(file_path)
            else:
                self._cache_file_value(file_path, force)
        self.dut.shell('echo \'{}\' > {}'.format(value, file_path))

    def read_thermal_value(self, file_path):
        """
        Read thermal related sys fs file content.
        :param file_path: Sys fs file name.
        :return: Content of sys fs file.
        """
        file_path = os.path.join(MockerHelper.THERMAL_PATH, file_path)
        return self.read_value(file_path)

    def read_led_value(self, file_path):
        """
        Read led related sys fs file content.
        :param file_path: Sys fs file name.
        :return: Content of sys fs file.
        """
        file_path = os.path.join(MockerHelper.LED_PATH, file_path)
        return self.read_value(file_path)

    def read_value(self, file_path):
        """
        Read sys fs file content.
        :param file_path: Sys fs file path.
        :return: Content of sys fs file.
        """
        out = self.dut.stat(path=file_path)
        if not out['stat']['exists']:
            raise SysfsNotExistError('{} not exist'.format(file_path))
        try:
            output = self.dut.command("cat %s" % file_path)
            value = output["stdout"]
            return value.strip()
        except Exception as e:
            assert 0, "Get content from %s failed, exception: %s" % (
                file_path, repr(e))

    def _cache_file_value(self, file_path, may_nexist=False):
        """
        Cache file value for regular file.
        :param file_path: Regular file path.
        :return:
        """
        try:
            output = self.dut.command("cat %s" % file_path)
            value = output["stdout"]
            self.regular_file_list[file_path] = value.strip()
        except Exception as e:
            if may_nexist:
                self.regular_file_list[file_path] = None
            else:
                assert 0, "Get content from %s failed, exception: %s" % (
                    file_path, repr(e))

    def _unlink(self, file_path):
        """
        Unlink given sys fs file, record its soft link target.
        :param file_path: Sys fs file path.
        :return:
        """
        readlink_output = self.dut.command('readlink {}'.format(file_path))
        self.unlink_file_list[file_path] = readlink_output["stdout"]
        self.dut.command('unlink {}'.format(file_path))
        self.dut.command('touch {}'.format(file_path))
        self.dut.command('chown admin {}'.format(file_path))

    def deinit(self):
        """
        Destructor of MockerHelper. Re-link all sys fs files.
        :return:
        """
        failed_recover_links = {}
        for file_path, link_target in list(self.unlink_file_list.items()):
            try:
                self.dut.command(
                    'ln -f -s {} {}'.format(link_target, file_path))
            except Exception:
                # Catch any exception for later retry
                failed_recover_links[file_path] = link_target

        failed_recover_files = {}
        for file_path, value in list(self.regular_file_list.items()):
            try:
                if value is None:
                    self.dut.shell('rm -f {}'.format(file_path))
                else:
                    self.dut.shell('echo \'{}\' > {}'.format(value, file_path))
            except Exception:
                # Catch any exception for later retry
                failed_recover_files[file_path] = value

        self.unlink_file_list.clear()
        self.regular_file_list.clear()
        # If there is any failed recover files, retry it
        if failed_recover_links or failed_recover_files:
            self.deinit_retry -= 1
            if self.deinit_retry > 0:
                self.unlink_file_list = failed_recover_links
                self.regular_file_list = failed_recover_files
                # The failed files might be used by other sonic daemons, delay 1 second
                # here to avoid conflict
                time.sleep(1)
                self.deinit()
            else:
                # We don't want to retry it infinite, and 5 times retry
                # is enough, so if it still fails after the retry, it
                # means there is probably an issue with our sysfs, we need
                # mark it fail here
                failed_recover_files.update(failed_recover_links)
                error_message = "Failed to recover all files, failed files: {}".format(
                    failed_recover_files)
                logging.error(error_message)
                raise RuntimeError(error_message)

    def is_201911(self):
        """
        Workaround to make thermal control test cases compatible with 201911 and master
        :return:
        """
        if parse_version(self.dut.kernel_version) > parse_version('4.9.0'):
            return False
        else:
            return True


class FanDrawerData:
    """
    Data mocker of a FAN drawer.
    """

    # FAN direction sys fs path available in 201911 and later
    FAN_DIR_PATH_ALL_FANS = '/run/hw-management/system/fan_dir'

    # FAN direction sys fs path available in 202012 and later
    FAN_DIR_PATH_PER_FAN = '/run/hw-management/thermal/fan{}_dir'

    FAN_EXPECT_DIRECTION = None

    def __init__(self, mock_helper, naming_rule, index):
        """
        Constructor of FAN drawer data.
        :param mock_helper: Instance of MockHelper.
        :param naming_rule: Naming rule of this kind of Fan drawer.
        :param index: Fan drawer index.
        """
        self.index = index
        self.helper = mock_helper
        self.platform_data = get_platform_data(self.helper.dut)
        if "201911" in self.helper.dut.os_version:
            self.mock_fan_direction = self.mock_fan_direction_fan_dir_for_all_fans
        else:
            self.mock_fan_direction = self.mock_fan_direction_fan_dir_per_fan
        if self.platform_data['fans']['hot_swappable']:
            self.name = 'drawer{}'.format(index)
        else:
            self.name = 'N/A'
        self.fan_data_list = []
        self.mocked_presence = None
        self.mocked_direction = None
        if 'presence' in naming_rule:
            self.presence_file = naming_rule['presence'].format(index)
        else:
            self.presence_file = None

        if 'led_capability' in naming_rule:
            self.led_capability_file = naming_rule['led_capability'].format(
                index)
        else:
            self.led_capability_file = None

        if 'led_green' in naming_rule:
            self.led_green_file = naming_rule['led_green'].format(index)
        else:
            self.led_green_file = None

        if 'led_red' in naming_rule:
            self.led_red_file = naming_rule['led_red'].format(index)
        else:
            self.led_red_file = None

        if 'led_orange' in naming_rule:
            self.led_orange_file = naming_rule['led_orange'].format(index)
        else:
            self.led_orange_file = None

    def mock_presence(self, presence):
        """
        Mock presence status of this FAN drawer.
        :param presence: Given presence value. 1 means present, 0 means not present.
        :return:
        """
        always_present = not self.platform_data['fans']['hot_swappable']
        if always_present:
            self.mocked_presence = 'Present'
        elif self.presence_file:
            self.helper.mock_thermal_value(self.presence_file, str(presence))
            self.mocked_presence = 'Present' if presence == 1 else 'Not Present'
        else:
            self.mocked_presence = 'Present'

    def mock_fan_direction_fan_dir_per_fan(self, direction):
        """
        Mock direction of this FAN with given direction value for the image where there is a fan_dir for each fan
        :param direction: Direction value. 1 means intake, 0 means exhaust.
        :return:
        """
        try:
            _ = int(self.helper.read_value(
                FanDrawerData.FAN_DIR_PATH_PER_FAN.format(self.index)))
        except SysfsNotExistError:
            self.mocked_direction = NOT_AVAILABLE
            return

        if direction:
            fan_dir_value = 1
            self.mocked_direction = 'intake'
        else:
            fan_dir_value = 0
            self.mocked_direction = 'exhaust'

        self.helper.mock_value(
            FanDrawerData.FAN_DIR_PATH_PER_FAN.format(self.index), fan_dir_value)

    def mock_fan_direction_fan_dir_for_all_fans(self, direction):
        """
        Mock direction of this FAN with given direction value for the image where there is only a fan_dir for all fans
        :param direction: Direction value. 1 means intake, 0 means exhaust.
        :return:
        """
        try:
            fan_dir_bits = int(self.helper.read_value(
                FanDrawerData.FAN_DIR_PATH_ALL_FANS))
        except SysfsNotExistError:
            self.mocked_direction = NOT_AVAILABLE
            return

        if direction:
            fan_dir_bits = fan_dir_bits | (1 << (self.index - 1))
            self.mocked_direction = 'intake'
        else:
            fan_dir_bits = fan_dir_bits & ~(1 << (self.index - 1))
            self.mocked_direction = 'exhaust'

        self.helper.mock_value(
            FanDrawerData.FAN_DIR_PATH_ALL_FANS, fan_dir_bits)

    def get_status_led(self):
        """
        Get led status from sys fs.
        :return: Led status read from sys fs.
        """
        led_capability = self.helper.read_led_value(self.led_capability_file)
        led_capability = led_capability.split()
        real_red_file = self.led_red_file if 'red' in led_capability else self.led_orange_file
        green_led_value = self.helper.read_led_value(self.led_green_file)
        red_led_value = self.helper.read_led_value(real_red_file)
        if green_led_value == '255' and red_led_value == '0':
            return 'green'
        elif green_led_value == '0' and red_led_value == '255':
            return 'red'
        else:
            assert 0, 'Invalid FAN led color for FAN: {}, green={}, red={}'.format(self.name, green_led_value,
                                                                                   red_led_value)

    def get_expect_led_color(self):
        if self.mocked_presence == 'Not Present':
            return 'red'

        for fan_data in self.fan_data_list:
            if fan_data.get_expect_led_color() == 'red':
                return 'red'

        return 'green'

    def mock_fan_direction_status(self, good, drawer_num):
        if FanDrawerData.FAN_EXPECT_DIRECTION is None:
            try:
                FanDrawerData.FAN_EXPECT_DIRECTION = int(self.helper.read_value(
                    FanDrawerData.FAN_DIR_PATH_PER_FAN.format(1)))
            except SysfsNotExistError:
                return None

        if good:
            target_dir = FanDrawerData.FAN_EXPECT_DIRECTION
        else:
            target_dir = 1 if FanDrawerData.FAN_EXPECT_DIRECTION == 0 else 0

        self.helper.mock_value(FanDrawerData.FAN_DIR_PATH_PER_FAN.format(drawer_num), str(target_dir))
        platform_data = get_platform_data(self.helper.dut)
        if platform_data['fans']['hot_swappable']:
            return FAN_NAMING_RULE['fan']['name'].format(drawer_num * MockerHelper.FAN_NUM_PER_DRAWER)
        else:
            return FAN_NAMING_RULE['fan']['name'].format(drawer_num)


class FanData:
    """
    Data mocker of a FAN.
    """

    # MAX PWM value.
    PWM_MAX = 255

    # Speed tolerance
    SPEED_TOLERANCE = 0.5

    # Cooling cur state file
    COOLING_CUR_STATE_FILE = 'cooling_cur_state'

    def __init__(self, mock_helper, naming_rule, index):
        """
        Constructor of FAN data.
        :param mock_helper: Instance of MockHelper.
        :param naming_rule: Naming rule of this kind of Fan.
        :param index: Fan index.
        """
        self.index = index
        self.helper = mock_helper
        self.name = naming_rule['name'].format(index)
        self.speed_file = naming_rule['speed'].format(index)
        self.mocked_speed = None
        self.mocked_target_speed = None
        self.mocked_status = None

        if 'target_speed' in naming_rule:
            self.target_speed_file = naming_rule['target_speed'].format(index)
        else:
            self.target_speed_file = None

        if 'max_speed' in naming_rule:
            self.max_speed_file = naming_rule['max_speed'].format(index)
        else:
            self.max_speed_file = None

        if 'status' in naming_rule:
            self.status_file = naming_rule['status'].format(index)
        else:
            self.status_file = None

    def mock_speed(self, speed):
        """
        Mock speed value of this FAN with given speed value.
        :param speed: Speed value in percentage.
        :return:
        """
        max_speed = self.get_max_speed()
        if max_speed > 0:
            speed_in_rpm = int(round(float(max_speed) * speed / 100))
            self.helper.mock_thermal_value(self.speed_file, str(speed_in_rpm))
        else:
            self.helper.mock_thermal_value(self.speed_file, str(speed))
        self.mocked_speed = speed

    def mock_target_speed(self, target_speed):
        """
        Mock target speed value of this FAN with given target speed value.
        :param target_speed: Target speed value in percentage.
        :return:
        """
        if self.target_speed_file:
            pwm = int(round(FanData.PWM_MAX * target_speed / 100.0))
            self.helper.mock_thermal_value(self.target_speed_file, str(pwm))
            self.mocked_target_speed = str(target_speed)
        else:
            self.mocked_target_speed = self.helper.read_thermal_value(
                self.speed_file)

    def mock_status(self, status):
        """
        Mock status of this FAN with given status value.
        :param status: Status value. 1 means OK, 0 means Not OK.
        :return:
        """
        if self.status_file:
            self.helper.mock_thermal_value(self.status_file, str(status))
            self.mocked_status = 'OK' if status == 0 else 'Not OK'
        else:
            self.mocked_status = 'OK'

    @classmethod
    def mock_cooling_cur_state(cls, mock_helper, value):
        mock_helper.mock_thermal_value(cls.COOLING_CUR_STATE_FILE, str(value))

    def get_max_speed(self):
        """
        Get max speed of this FAN.
        :return: Max speed of this FAN or -1 if max speed is not available.
        """
        if self.max_speed_file:
            max_speed = self.helper.read_thermal_value(self.max_speed_file)
            return int(max_speed)
        else:
            return -1

    def get_target_speed(self):
        """
        Get target speed of this FAN.
        :return: Target speed in percentage.
        """
        pwm = self.helper.read_thermal_value(self.target_speed_file)
        pwm = int(pwm)
        target_speed = int(round(pwm * 100.0 / FanData.PWM_MAX))
        return target_speed

    def get_expect_led_color(self):
        """
        Get expect LED color.
        :return: Return the LED color that this FAN expect to have.
        """
        if self.mocked_status == 'Not OK':
            return 'red'

        target_speed = self.get_target_speed()
        mocked_speed = int(self.mocked_speed)
        if mocked_speed > target_speed * (1 + FanData.SPEED_TOLERANCE):
            return 'red'

        if mocked_speed < target_speed * (1 - FanData.SPEED_TOLERANCE):
            return 'red'

        return 'green'

    def mock_psu_fan_dir(self, direction):
        try:
            dir_file = 'psu{}_fan_dir'.format(self.index)
            self.helper.mock_thermal_value(dir_file, str(direction))
            return 'intake' if direction == 1 else 'exhaust'
        except SysfsNotExistError:
            return NOT_AVAILABLE


class TemperatureData:
    """
    Data mocker of a thermal.
    """

    # Default high threshold value for generating mocked value.
    DEFAULT_HIGH_THRESHOLD = 80

    def __init__(self, mock_helper, naming_rule, index):
        """
        Constructor of FAN data.
        :param mock_helper: Instance of MockHelper.
        :param naming_rule: Naming rule of this kind of thermal.
        :param index: Thermal index.
        """
        self.helper = mock_helper
        patch_thermal_rule(mock_helper)
        self.name = naming_rule['name']
        self.temperature_file = naming_rule['temperature']
        self.high_threshold_file = naming_rule['high_threshold'] if 'high_threshold' in naming_rule else None
        self.high_critical_threshold_file = naming_rule[
            'high_critical_threshold'] if 'high_critical_threshold' in naming_rule else None
        if index is not None:
            self.name = self.name.format(index)
            self.temperature_file = self.temperature_file.format(index)
            if self.high_threshold_file:
                self.high_threshold_file = self.high_threshold_file.format(
                    index)
            if self.high_critical_threshold_file:
                self.high_critical_threshold_file = self.high_critical_threshold_file.format(
                    index)
        self.mocked_temperature = None
        self.mocked_high_threshold = None
        self.mocked_high_critical_threshold = None

    def mock_temperature(self, temperature):
        """
        Mock temperature value of this thermal with given value.
        :param temperature: Temperature value.
        :return:
        """
        self.helper.mock_thermal_value(self.temperature_file, str(temperature))
        temperature = temperature / float(1000)
        self.mocked_temperature = str(temperature)

    def get_high_threshold(self):
        """
        Get high threshold value of this thermal.
        :return: High threshold value of this thermal.
        """
        if self.high_threshold_file:
            high_threshold = self.helper.read_thermal_value(
                self.high_threshold_file)
            return int(high_threshold) / float(1000)
        else:
            return TemperatureData.DEFAULT_HIGH_THRESHOLD

    def mock_high_threshold(self, high_threshold):
        """
        Mock temperature high threshold value of this thermal with given value.
        :param high_threshold: High threshold value.
        :return:
        """
        if self.high_threshold_file:
            self.helper.mock_thermal_value(
                self.high_threshold_file, str(high_threshold))
            self.mocked_high_threshold = str(high_threshold / float(1000))
        else:
            self.mocked_high_threshold = NOT_AVAILABLE

    def mock_high_critical_threshold(self, high_critical_threshold):
        """
        Mock temperature high critical threshold value of this thermal with given value.
        :param high_critical_threshold: High critical threshold value.
        :return:
        """
        if self.high_critical_threshold_file:
            self.helper.mock_thermal_value(
                self.high_critical_threshold_file, str(high_critical_threshold))
            self.mocked_high_critical_threshold = str(
                high_critical_threshold / float(1000))
        else:
            self.mocked_high_critical_threshold = NOT_AVAILABLE


class CheckMockerResultMixin(object):

    def check_result(self, actual_data):
        """
        Check actual data with mocked data.
        :param actual_data: A list of dictionary contains actual command line data.

        :return: True if match else False.
        """
        expected = {}
        for name, fields in list(self.expected_data.items()):
            data = {}
            for idx, header in enumerate(self.expected_data_headers):
                data[header] = fields[idx]
            expected[name] = data

        logging.info("Expected: {}".format(json.dumps(expected, indent=2)))
        logging.info("Actual: {}".format(json.dumps(actual_data, indent=2)))

        extra_in_actual_data = []
        mismatch_in_actual_data = []
        for actual_data_item in actual_data:
            primary = actual_data_item[self.primary_field]
            if primary not in expected:
                extra_in_actual_data.append(actual_data_item)
            else:
                for field in list(actual_data_item.keys()):
                    if field in self.excluded_fields:
                        continue
                    if actual_data_item[field] != expected[primary][field]:
                        mismatch_in_actual_data.append(actual_data_item)
                        break
                expected.pop(primary)

        result = True
        if len(extra_in_actual_data) > 0:
            logging.error('Found extra data in actual_data: {}'
                          .format(json.dumps(extra_in_actual_data, indent=2)))
            result = False
        if len(mismatch_in_actual_data) > 0:
            logging.error('Found mismatch data in actual_data: {}'
                          .format(json.dumps(mismatch_in_actual_data, indent=2)))
            result = False
        if len(list(expected.keys())) > 0:
            logging.error('Expected data not found in actual_data: {}'
                          .format(json.dumps(expected, indent=2)))
            result = False

        return result


@mocker('FanStatusMocker')
class RandomFanStatusMocker(CheckMockerResultMixin, FanStatusMocker):
    """
    Mocker class to help generate random FAN status and check it with actual data.
    """

    # Max PSU FAN speed for generate random data only.
    PSU_FAN_MAX_SPEED = 10000

    def __init__(self, dut):
        """
        Constructor of RandomFanStatusMocker.
        :param dut: DUT object representing a SONiC switch under test.
        """
        FanStatusMocker.__init__(self, dut)
        self.mock_helper = MockerHelper(dut)
        self.drawer_list = []
        self.expected_data = {}
        self.expected_data_headers = [
            'drawer', 'led', 'fan', 'speed', 'direction', 'presence', 'status']
        self.primary_field = 'fan'
        self.excluded_fields = ['timestamp', ]

    def deinit(self):
        """
        Destructor of RandomFanStatusMocker.
        :return:
        """
        self.mock_helper.deinit()

    def mock_data(self):
        """
        Mock random data for all FANs in this DUT.
        :return:
        """
        fan_index = 1
        drawer_index = 1
        drawer_data = None
        presence = 0
        naming_rule = FAN_NAMING_RULE['fan']
        # All system fan is controlled to have the same speed, so only
        # get a random value once here
        speed = random.randint(60, 100)
        if not self.mock_helper.dut.is_host_service_running("hw-management-tc"):
            # When image doesn't support new tc, we still use cooling level to control thermal
            FanData.mock_cooling_cur_state(self.mock_helper, speed / 10)
        while fan_index <= MockerHelper.FAN_NUM:
            try:
                if (fan_index - 1) % MockerHelper.FAN_NUM_PER_DRAWER == 0:
                    drawer_data = FanDrawerData(
                        self.mock_helper, naming_rule, drawer_index)
                    self.drawer_list.append(drawer_data)
                    drawer_index += 1
                    presence = random.randint(0, 1)
                    drawer_data.mock_presence(presence)
                    drawer_data.mock_fan_direction(random.randint(0, 1))
                    if drawer_data.mocked_presence == 'Present':
                        presence = 1

                fan_data = FanData(self.mock_helper, naming_rule, fan_index)
                drawer_data.fan_data_list.append(fan_data)
                fan_index += 1
                if presence == 1:
                    fan_data.mock_status(random.randint(0, 1))
                    fan_data.mock_speed(speed)
                    fan_data.mock_target_speed(speed)
                    self.expected_data[fan_data.name] = [
                        drawer_data.name,
                        'N/A',  # update this value later
                        fan_data.name,
                        '{}%'.format(fan_data.mocked_speed),
                        drawer_data.mocked_direction,
                        drawer_data.mocked_presence,
                        fan_data.mocked_status
                    ]
                else:
                    self.expected_data[fan_data.name] = [
                        drawer_data.name,
                        'red',
                        fan_data.name,
                        'N/A',
                        'N/A',
                        'Not Present',
                        'N/A'
                    ]
            except SysfsNotExistError as e:
                logging.info('Failed to mock fan data: {}'.format(e))
                continue

        # update led color here
        for drawer_data in self.drawer_list:
            for fan_data in drawer_data.fan_data_list:
                if drawer_data.mocked_presence == 'Present':
                    expected_data = self.expected_data[fan_data.name]
                    expected_data[1] = drawer_data.get_expect_led_color()

        platform_data = get_platform_data(self.dut)
        if not platform_data['fans']['hot_swappable']:
            # For non swappable fan, all fans share one led
            is_one_red_led_at_least = False
            for _, expected_data in self.expected_data.items():
                if expected_data[1] == "red":
                    is_one_red_led_at_least = True
                    break
            if is_one_red_led_at_least:
                logging.info("update all expected led to red")
                for fan_name, expected_data in self.expected_data.items():
                    self.expected_data[fan_name][1] = "red"

        platform_data = get_platform_data(self.mock_helper.dut)
        psu_count = platform_data["psus"]["number"]
        naming_rule = FAN_NAMING_RULE['psu_fan']
        if self.mock_helper.is_201911():
            led_color = ''
            naming_rule['name'] = 'psu_{}_fan_1'
        else:
            led_color = 'green'
        check_psu_fan_dir_cmd = 'sonic-db-cli STATE_DB HGET "FAN_INFO|psu1_fan1" direction'
        support_psu_fan_dir = self.mock_helper.dut.shell(check_psu_fan_dir_cmd)['stdout'].strip() != 'N/A'
        for index in range(1, psu_count + 1):
            try:
                fan_data = FanData(self.mock_helper, naming_rule, index)
                fan_data.mock_speed(speed)

                self.expected_data[fan_data.name] = [
                    'N/A',
                    led_color,
                    fan_data.name,
                    '{}%'.format(fan_data.mocked_speed),
                    fan_data.mock_psu_fan_dir(random.randint(0, 1)) if support_psu_fan_dir else NOT_AVAILABLE,
                    'Present',
                    'OK'
                ]
            except SysfsNotExistError as e:
                logging.info(
                    'Failed to mock fan data for {} - {}'.format(fan_data.name, e))
                continue

    def check_all_fan_speed(self, expected_speed):
        """
        Check all FAN speed match the given expect speed.
        :param expected_speed: Expect speed in percentage.
        :return: True if match else False.
        """
        for drawer_data in self.drawer_list:
            for fan_data in drawer_data.fan_data_list:
                if fan_data.target_speed_file:
                    target_speed = fan_data.get_target_speed()
                    if expected_speed != target_speed:
                        logging.error(
                            '{} expected speed={}, actual speed={}'.format(fan_data.name, expected_speed, target_speed))
                        return False
        return True


@mocker('ThermalStatusMocker')
class RandomThermalStatusMocker(CheckMockerResultMixin, ThermalStatusMocker):
    """
    RandomThermalStatusMocker class to help generate random thermal status and check it with actual data.
    """

    # Default threshold diff between high threshold and critical threshold
    DEFAULT_THRESHOLD_DIFF = 5

    def __init__(self, dut):
        """
        Constructor of RandomThermalStatusMocker.
        :param dut: DUT object representing a SONiC switch under test.
        """
        ThermalStatusMocker.__init__(self, dut)
        self.mock_helper = MockerHelper(dut)
        self.expected_data = {}
        self.expected_data_headers = ['sensor', 'temperature', 'high th', 'low th', 'crit high th', 'crit low th',
                                      'warning']
        self.primary_field = 'sensor'
        self.excluded_fields = ['timestamp', ]

    def deinit(self):
        """
        Destructor of RandomThermalStatusMocker.
        :return:
        """
        self.mock_helper.deinit()

    def mock_data(self):
        """
        Mock random data for all Thermals in this DUT.
        :return:
        """
        platform_data = get_platform_data(self.mock_helper.dut)
        thermal_dict = platform_data["thermals"]
        for category, content in list(thermal_dict.items()):
            number = int(content['number'])
            naming_rule = THERMAL_NAMING_RULE[category]
            if 'start' in content:
                start = int(content['start'])
                for index in range(start, start + number):
                    mock_data = TemperatureData(
                        self.mock_helper, naming_rule, index)
                    self._do_mock(mock_data)
            else:  # non index-able thermal
                mock_data = TemperatureData(
                    self.mock_helper, naming_rule, None)
                self._do_mock(mock_data)

    def _do_mock(self, mock_data):
        """
        Mock random data for a thermal in the DUT.
        :param mock_data: An instance of TemperatureData.
        :return:
        """
        try:
            high_threshold = mock_data.get_high_threshold()
            if high_threshold != 0:
                temperature = random.randint(
                    1, high_threshold - RandomThermalStatusMocker.DEFAULT_THRESHOLD_DIFF)
                mock_data.mock_temperature(temperature)

                high_threshold = temperature + RandomThermalStatusMocker.DEFAULT_THRESHOLD_DIFF
                mock_data.mock_high_threshold(high_threshold)

                high_critical_threshold = high_threshold + \
                    RandomThermalStatusMocker.DEFAULT_THRESHOLD_DIFF
                mock_data.mock_high_critical_threshold(high_critical_threshold)
            else:
                mock_data.mocked_temperature = NOT_AVAILABLE
                mock_data.mocked_high_threshold = NOT_AVAILABLE
                mock_data.mocked_high_critical_threshold = NOT_AVAILABLE

            self.expected_data[mock_data.name] = [
                mock_data.name,
                mock_data.mocked_temperature,
                mock_data.mocked_high_threshold,
                NOT_AVAILABLE,
                mock_data.mocked_high_critical_threshold,
                NOT_AVAILABLE,
                'False'
            ]
        except SysfsNotExistError as e:
            logging.info(
                'Failed to mock thermal data for {} - {}'.format(mock_data.name, e))

    def check_thermal_algorithm_status(self, expected_status):
        """
        Deprecated.
        Check if actual thermal algorithm status match given expected value.
        :param expected_status: True if enable else False.
        :return: True if match else False
        """
        return True


@mocker('SingleFanMocker')
class AbnormalFanMocker(SingleFanMocker):
    """
    Mocker to generate abnormal FAN status.
    """

    # Speed tolerance value
    SPEED_TOLERANCE = 50

    # Speed value
    TARGET_SPEED_VALUE = 60

    def __init__(self, dut):
        """
        Constructor of AbnormalFanMocker
        :param dut: DUT object representing a SONiC switch under test.
        """
        SingleFanMocker.__init__(self, dut)
        self.mock_helper = MockerHelper(dut)
        naming_rule = FAN_NAMING_RULE['fan']
        self.fan_data_list = []
        self.fan_drawer_data_list = []
        fan_index = 1
        drawer_index = 1
        while fan_index <= MockerHelper.FAN_NUM:
            if (fan_index - 1) % MockerHelper.FAN_NUM_PER_DRAWER == 0:
                self.fan_drawer_data_list.append(FanDrawerData(
                    self.mock_helper, naming_rule, drawer_index))
                drawer_index += 1
            self.fan_data_list.append(
                FanData(self.mock_helper, naming_rule, fan_index))
            fan_index += 1
        self.fan_data = self.fan_data_list[0]
        self.fan_drawer_data = self.fan_drawer_data_list[0]
        self.expect_led_color = None
        self.mock_all_normal()

    def deinit(self):
        """
        Destructor of AbnormalFanMocker.
        :return:
        """
        self.mock_helper.deinit()

    def check_result(self, actual_data):
        """
        Check actual data with mocked data.
        :param actual_data: A list of dictionary contains actual command line data.

        :return: True if a match line is found.
        """
        for fan in actual_data:
            if fan['fan'] == self.fan_data.name:
                try:
                    actual_color = self.fan_drawer_data.get_status_led()
                    if actual_color != self.expect_led_color:
                        logging.error('FAN {} color is {}, expect: {}'.format(fan['fan'], actual_color,
                                                                              self.expect_led_color))
                        return False
                except SysfsNotExistError as e:
                    logging.info(
                        'LED check only support on SPC2 and SPC3: {}'.format(e))
                return True

        logging.error('Expected data not found')
        return False

    def is_fan_removable(self):
        """
        :return: True if FAN is removable else False
        """
        platform_data = get_platform_data(self.mock_helper.dut)
        return platform_data['fans']['hot_swappable']

    def mock_all_normal(self):
        """
        Change all the mocked FANs status to normal.
        :return:
        """
        for drawer_data in self.fan_drawer_data_list:
            try:
                drawer_data.mock_presence(1)
            except SysfsNotExistError as e:
                logging.info('Failed to mock drawer data: {}'.format(e))

        for fan_data in self.fan_data_list:
            try:
                fan_data.mock_status(0)
                fan_data.mock_speed(AbnormalFanMocker.TARGET_SPEED_VALUE)
                fan_data.mock_target_speed(
                    AbnormalFanMocker.TARGET_SPEED_VALUE)
            except SysfsNotExistError as e:
                logging.info('Failed to mock fan data: {}'.format(e))

    def mock_normal(self):
        """
        Change the mocked FAN status to 'Present' and normal speed.
        :return:
        """
        self.mock_status(0)
        self.mock_presence()
        self.mock_normal_speed()

    def mock_absence(self):
        """
        Change the mocked FAN status to 'Not Present'.
        :return:
        """
        self.fan_drawer_data.mock_presence(0)
        self.expect_led_color = 'red'

    def mock_presence(self):
        """
        Change the mocked FAN status to 'Present'
        :return:
        """
        self.fan_drawer_data.mock_presence(1)
        self.expect_led_color = 'green'

    def mock_status(self, status):
        """
        Change the mocked FAN status to good or bad
        :param status: bool value indicate the target status of the FAN.
        :return:
        """
        self.fan_data.mock_status(0 if status else 1)
        self.expect_led_color = 'green' if status else 'red'

    def mock_over_speed(self):
        """
        Change the mocked FAN speed to faster than target speed and exceed speed tolerance.
        :return:
        """
        self.fan_data.mock_speed(
            AbnormalFanMocker.TARGET_SPEED_VALUE * (100 + AbnormalFanMocker.SPEED_TOLERANCE) / 100 + 10)
        self.fan_data.mock_target_speed(AbnormalFanMocker.TARGET_SPEED_VALUE)
        self.expect_led_color = 'red'

    def mock_under_speed(self):
        """
        Change the mocked FAN speed to slower than target speed and exceed speed tolerance.
        :return:
        """
        self.fan_data.mock_speed(
            AbnormalFanMocker.TARGET_SPEED_VALUE * (100 - AbnormalFanMocker.SPEED_TOLERANCE) / 100 - 10)
        self.fan_data.mock_target_speed(AbnormalFanMocker.TARGET_SPEED_VALUE)
        self.expect_led_color = 'red'

    def mock_normal_speed(self):
        """
        Change the mocked FAN speed to a normal value.
        :return:
        """
        self.fan_data.mock_speed(AbnormalFanMocker.TARGET_SPEED_VALUE)
        self.fan_data.mock_target_speed(AbnormalFanMocker.TARGET_SPEED_VALUE)
        self.expect_led_color = 'green'


@mocker('MinTableMocker')
class MinTableMocker(object):
    FAN_AMB_PATH = 'fan_amb'
    PORT_AMB_PATH = 'port_amb'
    TRUST_PATH = 'module1_temp_fault'
    LIST_THERMAL_ZONE_TEMPERATURE_FILE = 'ls /run/hw-management/thermal/mlxsw*/thermal_zone_temp'
    NORMAL_TEMPERATURE = 40000

    def __init__(self, dut):
        self.mock_helper = MockerHelper(dut)

    def get_expect_cooling_level(self, temperature, trust_state):
        minimum_table = get_min_table(self.mock_helper.dut)
        row = minimum_table['unk_{}'.format(
            'trust' if trust_state else 'untrust')]
        temperature = temperature / 1000
        for range_str, cooling_level in list(row.items()):
            range_str_list = range_str.split(':')
            min_temp = int(range_str_list[0])
            max_temp = int(range_str_list[1])
            if min_temp <= temperature <= max_temp:
                return cooling_level - 10

        return None

    def mock_min_table(self, temperature, trust_state):
        trust_value = '0' if trust_state else '1'
        fan_temp = temperature
        port_temp = temperature

        self.mock_helper.mock_thermal_value(self.FAN_AMB_PATH, str(fan_temp))
        self.mock_helper.mock_thermal_value(self.PORT_AMB_PATH, str(port_temp))
        self.mock_helper.mock_thermal_value(self.TRUST_PATH, str(trust_value))

    def mock_normal_temperature(self):
        output = self.mock_helper.dut.shell(
            self.LIST_THERMAL_ZONE_TEMPERATURE_FILE)
        for thermal_file in output['stdout_lines']:
            if self.mock_helper.read_value(thermal_file) != '0':
                self.mock_helper.mock_value(
                    thermal_file, self.NORMAL_TEMPERATURE)

    def deinit(self):
        """
        Destructor of MinTableMocker.
        :return:
        """
        self.mock_helper.deinit()


@mocker('PsuMocker')
class PsuMocker(object):
    PSU_PRESENCE = 'psu{}_status'

    def __init__(self, dut):
        self.mock_helper = MockerHelper(dut)

    def deinit(self):
        """
        Destructor of PsuMocker.
        :return:
        """
        self.mock_helper.deinit()

    def mock_psu_status(self, psu_index, status):
        self.mock_helper.mock_thermal_value(
            self.PSU_PRESENCE.format(psu_index), '1' if status else '0')


@mocker('CpuThermalMocker')
class CpuThermalMocker(object):
    LOW_THRESHOLD = 80000
    HIGH_THRESHOLD = 95000
    MIN_COOLING_STATE = 2
    MAX_COOLING_STATE = 10
    CPU_COOLING_STATE_FILE = '/var/run/hw-management/thermal/cooling2_cur_state'
    CPU_PACK_TEMP_FILE = '/var/run/hw-management/thermal/cpu_pack'

    def __init__(self, dut):
        self.mock_helper = MockerHelper(dut)

    def deinit(self):
        """
        Destructor of CpuThermalMocker.
        :return:
        """
        self.mock_helper.deinit()

    def mock_cpu_pack_temperature(self, temperature):
        self.mock_helper.mock_value(self.CPU_PACK_TEMP_FILE, temperature)

    def get_cpu_cooling_state(self):
        return int(self.mock_helper.read_value(self.CPU_COOLING_STATE_FILE))


@mocker('PsuPowerThresholdMocker')
class PsuPowerThresholdMocker(object):
    PORT_AMBIENT_TEMP = '/var/run/hw-management/thermal/port_amb'
    FAN_AMBIENT_TEMP = '/var/run/hw-management/thermal/fan_amb'
    AMBIENT_TEMP_CRITICAL_THRESHOLD = '/var/run/hw-management/config/amb_tmp_crit_limit'
    AMBIENT_TEMP_WARNING_THRESHOLD = '/var/run/hw-management/config/amb_tmp_warn_limit'
    PSU_POWER_SLOPE = '/var/run/hw-management/config/psu{}_power_slope'
    PSU_POWER_CAPACITY = '/var/run/hw-management/config/psu{}_power_capacity'
    PSU_POWER = '/var/run/hw-management/power/psu{}_power'

    def __init__(self, dut):
        self.mock_helper = MockerHelper(dut)

    def deinit(self):
        self.mock_helper.deinit()

    def mock_power_threshold(self, number_psus):
        self.mock_helper.mock_value(
            self.AMBIENT_TEMP_WARNING_THRESHOLD, 38000, True)
        self.mock_helper.mock_value(
            self.AMBIENT_TEMP_CRITICAL_THRESHOLD, 40000, True)

        max_power = None
        for i in range(number_psus):
            if not max_power:
                power = int(self.mock_helper.read_value(
                    self.PSU_POWER.format(i + 1)))
                # Round up to 100 watt and then double it to avoid noise when power fluctuate
                max_power = int(round(power / 100000000.0)) * 100000000 * 2
            self.mock_helper.mock_value(
                self.PSU_POWER_CAPACITY.format(i + 1), max_power, True)
            self.mock_helper.mock_value(
                self.PSU_POWER_SLOPE.format(i + 1), 30, True)

        # Also mock ambient temperatures
        self.mock_helper.mock_value(
            self.PORT_AMBIENT_TEMP, self.read_port_ambient_thermal())
        self.mock_helper.mock_value(
            self.FAN_AMBIENT_TEMP, self.read_fan_ambient_thermal())

    def mock_psu_power(self, power, number_psus):
        for i in range(number_psus):
            self.mock_helper.mock_value(self.PSU_POWER.format(i+1), int(power/number_psus))

    def mock_fan_ambient_thermal(self, temperature):
        self.mock_helper.mock_value(self.FAN_AMBIENT_TEMP, int(temperature))

    def mock_port_ambient_thermal(self, temperature):
        self.mock_helper.mock_value(self.PORT_AMBIENT_TEMP, int(temperature))

    def mock_ambient_temp_critical_threshold(self, temperature):
        self.mock_helper.mock_value(self.AMBIENT_TEMP_CRITICAL_THRESHOLD, int(temperature))

    def mock_ambient_temp_warning_threshold(self, temperature):
        self.mock_helper.mock_value(self.AMBIENT_TEMP_WARNING_THRESHOLD, int(temperature))

    def read_psu_power_threshold(self, psu):
        return int(self.mock_helper.read_value(self.PSU_POWER_CAPACITY.format(psu)))

    def read_psu_power_slope(self, psu):
        return int(self.mock_helper.read_value(self.PSU_POWER_SLOPE.format(psu)))

    def read_psu_power(self, psu):
        return int(self.mock_helper.read_value(self.PSU_POWER.format(psu)))

    def read_ambient_temp_critical_threshold(self):
        return int(self.mock_helper.read_value(self.AMBIENT_TEMP_CRITICAL_THRESHOLD))

    def read_ambient_temp_warning_threshold(self):
        return int(self.mock_helper.read_value(self.AMBIENT_TEMP_WARNING_THRESHOLD))

    def read_port_ambient_thermal(self):
        return int(self.mock_helper.read_value(self.PORT_AMBIENT_TEMP))

    def read_fan_ambient_thermal(self):
        return int(self.mock_helper.read_value(self.FAN_AMBIENT_TEMP))


@mocker('RebootCauseMocker')
class RebootCauseMocker(object):
    RESET_RELOAD_BIOS = '/var/run/hw-management/system/reset_reload_bios'
    RESET_FROM_ASIC = '/var/run/hw-management/system/reset_from_asic'

    def __init__(self, dut):
        self.mock_helper = MockerHelper(dut)

    def deinit(self):
        self.mock_helper.deinit()

    def mock_reset_reload_bios(self):
        self.mock_helper.mock_value(self.RESET_RELOAD_BIOS, 1)

    def mock_reset_from_asic(self):
        self.mock_helper.mock_value(self.RESET_FROM_ASIC, 1)


def suspend_hw_tc_service(dut):
    """
    Suspend thermal control service
    """
    logging.info("suspend hw tc service ")

    dut.shell(f"sudo touch {SUSPEND_FILE_PATH}")
    dut.shell(f"sudo chown admin {SUSPEND_FILE_PATH}")
    dut.shell(f"sudo echo 1 > {SUSPEND_FILE_PATH}")

    def check_pwm_is_max():
        pwm = int(dut.shell("cat /var/run/hw-management/thermal/pwm1")["stdout"])
        return pwm == 255

    pytest_assert(wait_until(10, 0, 1, check_pwm_is_max), "TC is not suspended")


def resume_hw_tc_service(dut):
    """
    Resume hw thermal control service
    """
    logging.info("resume hw tc service ")
    dut.shell(f"sudo rm -f {SUSPEND_FILE_PATH}")
