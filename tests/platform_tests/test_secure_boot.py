"""
this tests checks secure boot upgrade

In order to run this test, you need to specify the following argument:
	1. --target_image_list (to contain your non secure image path e.g. /tmp/images/my_non_secure_img.bin)
e.g.:
(from tests dir)
	pytest platform_tests/test_secure_boot.py <regular arguments> --target_image_list non_secure_image.bin
"""
import logging
import pytest
import re
from tests.common.errors import RunAnsibleModuleFail
from tests.common.helpers.assertions import pytest_assert
from tests.upgrade_path.upgrade_helpers import install_sonic
from tests.upgrade_path.test_upgrade_path import upgrade_path_lists

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.sanity_check(skip_sanity=True),
    pytest.mark.disable_loganalyzer,
    pytest.mark.skip_check_dut_health
]

logger = logging.getLogger(__name__)


@pytest.fixture(scope='session', autouse=True)
def keep_same_version_installed(duthost):
    '''
    @summary: extract the current version installed as shown in the "show boot" output.
    and restore original image installed after the test run
    :param duthost: device under test
    :return: the version currently installed
    '''
    output = duthost.shell("show boot")['stdout']
    results = re.findall("Current\s*\:\s*(.*)\n", output)
    pytest_assert(len(results) > 0, "Current image is empty!")
    current_version = results[0]
    yield
    duthost.shell("sonic-installer set-default {}", format(current_version))


@pytest.fixture(scope='session')
def non_secure_image_path(upgrade_path_lists):
    '''
    @summary: will extract the non secure image path from --target_image_list parameter
    :return: given non secure image path
    '''
    _, _, non_secure_img_path, _ = upgrade_path_lists
    pytest_assert(len(non_secure_img_path) == 1, "Please specify one non-secure image path")
    return non_secure_img_path


def test_non_secure_boot_upgrade_failure(duthost, non_secure_image_path, tbinfo):
    """
    @summary: This test case validates non successful upgrade of a given non secure image
    """
    # install non secure image
    logger.info("install non secure image - expect fail, image path = {}".format(non_secure_image_path))
    result = "image install failure" # because we expect fail
    try:
        # in case of success result will take the target image name
        result = install_sonic(duthost, non_secure_image_path, tbinfo)
    except RunAnsibleModuleFail as err:
        err_msg = str(err.results._check_key("module_stdout"))
        logger.info("Expected fail, msg : {}".format(err_msg))
        pytest_assert("Failure: CMS signature verification failed" in str(err_msg), "failure was not due to security limitations")
    finally:
        pytest_assert(result == "image install failure", "non-secure image was successfully installed")
