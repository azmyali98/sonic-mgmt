'''
This test case checks default password change after initial reboot.
Due to new law passed in California, each default user must change their default password.

Important Note:
    Please run this test from sonic-mgmt/tests folder, otherwise it will fail.
'''
import pytest
import logging
import pexpect
import time
from tests.common.helpers.assertions import pytest_assert
from tests.platform_tests.test_first_time_boot_password_change.default_consts import DefaultConsts
from tests.platform_tests.test_first_time_boot_password_change.manufacture import manufacture

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.sanity_check(skip_sanity=True),
    pytest.mark.disable_loganalyzer,
    pytest.mark.skip_check_dut_health
]


class currentConfigurations:
    '''
    @summary: this class will act as a global database to save current configurations and changes the test made.
    It will help us track the current state of the system,
    and we will be used as part of cleanup fixtures.
    '''
    def __init__(self):
        self.currentPassword = DefaultConsts.DEFAULT_PASSWORD  # initial password


logger = logging.getLogger(__name__)
currentConfigurations = currentConfigurations()


@pytest.fixture(scope='module', autouse=True)
def dut_hostname(request):
    '''
    @summary: this function returns the hostname of the dut from the 'host-pattern'
    '''
    hostname = request.config.getoption('--host-pattern')
    logger.info("Hostname is {}".format(hostname))
    return hostname


@pytest.fixture(scope='module', autouse=True)
def prepare_system_for_first_boot(request, dut_hostname):
    '''
    @summary: will manufacture the dut device to the given image in the parameter --restore_to_image,
    by installing the image given from ONIE. for detailed information read the documentation
    of the manufacture script.
    '''
    restore_image_path = request.config.getoption('restore_to_image')
    pytest_assert(restore_image_path is not None, "restore_to_image param is empty, Please specify path to an image")
    manufacture(dut_hostname, restore_image_path)


def change_password(dut_hostname, username, current_password, new_password):
    '''
    @summary: this function changes the password for the user given
    :param dut_hostname: host name of the dut
    :param dut_ip: device under test
    :param username: user name to change the password for
    :param current_password: current password
    :param new_password: new password
    '''
    logger.info("Changing password for username:{} to password: {}".format(username, new_password))
    try:
        # create a new ssh connection
        engine = pexpect.spawn(DefaultConsts.SSH_COMMAND.format(username) + dut_hostname, timeout=15)
        # because of race condition
        engine.delaybeforesend = 0.2
        engine.delayafterclose = 0.5
        engine.expect(DefaultConsts.PASSWORD_REGEX)
        engine.sendline(current_password + '\r')
        engine.expect(DefaultConsts.SONIC_PROMPT)
        engine.sendline('sudo usermod -p $(openssl passwd -1 {}) {}'.format(new_password, username) + '\r')
        engine.expect(DefaultConsts.SONIC_PROMPT)
        logger.info("Sleeping for {} secs to apply password change".format(DefaultConsts.APPLY_CONFIGURATIONS))
        time.sleep(DefaultConsts.APPLY_CONFIGURATIONS)
        engine.sendline('exit')
        engine.close()
    except Exception as err:
        logger.info('Got an exception while changing the password')
        logger.info(str(err))


@pytest.fixture(scope='function', autouse=True)
def restore_original_password(dut_hostname):
    '''
    @summary: this function will restore the original password to the default one to allow
    the next test to use default password to login to dut.
    '''
    yield
    logger.info("Sleep {} secs for system stabilization".format(DefaultConsts.STABILIZATION_TIME))
    time.sleep(DefaultConsts.STABILIZATION_TIME)
    logger.info("Restore original password")
    change_password(dut_hostname,
                    DefaultConsts.DEFAULT_USER,
                    currentConfigurations.currentPassword,
                    DefaultConsts.DEFAULT_PASSWORD)


def test_default_password_change_after_first_boot(dut_hostname):
    '''
    @summary: in this test case we want to validate the mandatory request of
    password change after the first boot of the given image.
    According to a new law passed on the united states, default passwords
    such as: "admin", "root", "12345", etc. are no longer accepted.
    Test Flow:
        1.A message should appear after initial boot, requesting password change for default user.
        2.Password change, it will be tested by relogin to dut with new password and expecting no expire message again
    :param dut_hostname: name of device under test
    '''
    logger.info("create ssh connection to device after inital boot")
    engine = pexpect.spawn(DefaultConsts.SSH_COMMAND.format(DefaultConsts.DEFAULT_USER) + dut_hostname)
    # to prevent race condition
    engine.delaybeforesend = 0.2
    engine.delayafterclose = 0.5
    # it should require password so password will be sent
    engine.expect(DefaultConsts.PASSWORD_REGEX)
    engine.sendline(DefaultConsts.DEFAULT_PASSWORD)
    # we should expect the expired password regex to appear
    logger.info("Expecting expired message printed")
    index = engine.expect([DefaultConsts.EXPIRED_PASSWORD_MSG, pexpect.TIMEOUT])
    if index != 0:
        engine.close()
        raise Exception("We did not catch the message of expired password after initial boot!\n"
                        "Consider this as a bug or a degradation")
    logger.info('Entering current password after the expired message appeared')
    engine.sendline(DefaultConsts.DEFAULT_PASSWORD + '\r')
    # suggest new password
    logger.info('Entering a new password, password used is {}'.format(DefaultConsts.NEW_PASSWORD))
    engine.expect(DefaultConsts.NEW_PASSWORD_REGEX)
    engine.sendline(DefaultConsts.NEW_PASSWORD + '\r')
    logger.info('Retyping the new password')
    engine.expect(DefaultConsts.RETYPE_PASSWORD_REGEX)
    engine.sendline(DefaultConsts.NEW_PASSWORD + '\r')
    engine.expect(DefaultConsts.DEFAULT_PROMPT)
    # update global configuration database, it will be used in cleanup later
    currentConfigurations.currentPassword = DefaultConsts.NEW_PASSWORD
    logger.info("Exit cli for the default user and re-eneter again and expect no password expire message")
    # close the session
    engine.close()
    logger.info("Sleeping for {} secs to allow system update password".format(DefaultConsts.STABILIZATION_TIME))
    time.sleep(DefaultConsts.STABILIZATION_TIME)
    logger.info("create a new ssh connection to device")
    engine = pexpect.spawn(DefaultConsts.SSH_COMMAND + dut_hostname)
    engine.delaybeforesend = 0.2
    engine.delayafterclose = 0.5
    # expect password
    engine.expect(DefaultConsts.PASSWORD_REGEX)
    # enter new password
    engine.sendline(DefaultConsts.NEW_PASSWORD + '\r')
    # we should not expect the expired password regex to appear again
    index = engine.expect([DefaultConsts.EXPIRED_PASSWORD_MSG] + DefaultConsts.DEFAULT_PROMPT)
    if index == 0:
        engine.close()
        raise Exception("We captured the expiring message again after updating a new password!\n")
    engine.close()
