import os
import sys
from datetime import datetime

from automation_tools import install_prerequisites
from automation_tools import setup_satellite_firewall
from automation_tools import subscribe
from automation_tools.utils import update_packages
from fabric.api import env
from fabric.api import execute
from fabric.api import run
from robozilla.decorators import bz_bug_is_open

from upgrade.helpers import settings
from upgrade.helpers.constants.constants import CUSTOM_SAT_REPO
from upgrade.helpers.constants.constants import RHEL_CONTENTS
from upgrade.helpers.logger import logger
from upgrade.helpers.tasks import enable_disable_repo
from upgrade.helpers.tasks import foreman_maintain_package_update
from upgrade.helpers.tasks import foreman_packages_installation_check
from upgrade.helpers.tasks import foreman_service_restart
from upgrade.helpers.tasks import nonfm_upgrade
from upgrade.helpers.tasks import pulp2_pulp3_migration
from upgrade.helpers.tasks import repository_setup
from upgrade.helpers.tasks import setup_satellite_repo
from upgrade.helpers.tasks import upgrade_using_foreman_maintain
from upgrade.helpers.tasks import upgrade_validation
from upgrade.helpers.tasks import workaround_1967131
from upgrade.helpers.tasks import yum_repos_cleanup
from upgrade.helpers.tools import host_ssh_availability_check
from upgrade.helpers.tools import reboot

logger = logger()


def satellite_setup(satellite_host):
    """
    The purpose of this method to make the satellite ready for upgrade.
    :param satellite_host:
    :return: satellite_host
    """
    os.environ["RHN_USERNAME"] = settings.subscription.rhn_username
    os.environ["RHN_PASSWORD"] = settings.subscription.rhn_password
    os.environ["RHN_POOLID"] = settings.subscription.rhn_poolid
    execute(host_ssh_availability_check, satellite_host)
    execute(yum_repos_cleanup, host=satellite_host)
    execute(install_prerequisites, host=satellite_host)
    execute(subscribe, host=satellite_host)
    execute(foreman_service_restart, host=satellite_host)
    env['satellite_host'] = satellite_host
    settings.upgrade.satellite_hostname = satellite_host
    logger.info(f'Satellite {satellite_host} is ready for Upgrade!')
    return satellite_host


def satellite_upgrade(zstream=False):
    """This function is used to perform the satellite upgrade of two type based on
    their passed parameter.
    :param zstream:

    if upgrade_type==None:
        - Upgrades Satellite Server from old version to latest
            The following environment variables affect this command:

            SAT_UPGRADE_BASE_URL
                Optional, defaults to available satellite version in CDN.
                URL for the compose repository
            FROM_VERSION
                Current satellite version which will be upgraded to latest version
            TO_VERSION
                Satellite version to upgrade to and enable repos while upgrading.
                e.g '6.1','6.2', '6.3'
            FOREMAN_MAINTAIN_SATELLITE_UPGRADE
                use foreman-maintain for satellite upgrade

    else:
        - Upgrades Satellite Server to its latest zStream version
            Note: For zstream upgrade both 'To' and 'From' version should be same

            FROM_VERSION
                Current satellite version which will be upgraded to latest version
            TO_VERSION
                Next satellite version to which satellite will be upgraded
            FOREMAN_MAINTAIN_SATELLITE_UPGRADE
                use foreman-maintain for satellite upgrade

    """
    logger.highlight('\n========== SATELLITE UPGRADE =================\n')
    if zstream:
        if not settings.upgrade.from_version == settings.upgrade.to_version:
            logger.warning('zStream Upgrade on Satellite cannot be performed as '
                           'FROM and TO versions are not same!')
            sys.exit(1)
    major_ver = settings.upgrade.os[-1]
    common_sat_cap_repos = [
        RHEL_CONTENTS["rhscl"]["label"],
        RHEL_CONTENTS["server"]["label"]
    ]
    if settings.upgrade.downstream_fm_upgrade:
        settings.upgrade.whitelist_param = ", repositories-validate, repositories-setup"

    # disable all the repos
    enable_disable_repo(disable_repos_name=["*"])

    # It is required to enable the tools and server for non-fm upgrade because in
    # fm both the repos enabled by the fm tool.
    if not settings.upgrade.foreman_maintain_satellite_upgrade:
        enable_disable_repo(enable_repos_name=common_sat_cap_repos)
    if settings.upgrade.distribution == 'cdn':
        enable_disable_repo(enable_repos_name=['rhel-7-server-satellite-maintenance-6-rpms'])
    else:
        for repo in CUSTOM_SAT_REPO:
            repository_setup(
                CUSTOM_SAT_REPO[repo]["repository"],
                CUSTOM_SAT_REPO[repo]["repository_name"],
                CUSTOM_SAT_REPO[repo]["base_url"],
                CUSTOM_SAT_REPO[repo]["enable"],
                CUSTOM_SAT_REPO[repo]["gpg"]
            )
        foreman_maintain_package_update()
    if settings.upgrade.to_version == "6.10":
        if bz_bug_is_open(1967131):
            workaround_1967131(task_type="apply")
        pulp_migration_status = pulp2_pulp3_migration()
        if bz_bug_is_open(1967131):
            workaround_1967131()
        if not pulp_migration_status:
            sys.exit(1)

    if settings.upgrade.foreman_maintain_satellite_upgrade:
        preup_time = datetime.now().replace(microsecond=0)
        upgrade_using_foreman_maintain()
        postup_time = datetime.now().replace(microsecond=0)
        logger.highlight('Time taken for Satellite Upgrade - {}'.format(
            str(postup_time - preup_time)))
    else:
        # To install the package using foreman-maintain and it is applicable
        # above 6.7 version.
        setup_satellite_firewall()
        if not zstream:
            run('rm -rf /etc/yum.repos.d/rhel-{optional,released}.repo')
            logger.info('Updating system packages ... ')
            foreman_packages_installation_check(state="unlock")
            setup_satellite_repo()
            foreman_maintain_package_update()
            update_packages(quiet=True)

        if settings.upgrade.distribution == "cdn":
            enable_disable_repo(enable_repos_name=[f'rhel-{major_ver}-server-satellite'
                                                   f'-{settings.upgrade.to_version}-rpms'])
        nonfm_upgrade()
        foreman_packages_installation_check(state="lock")
    # Rebooting the satellite for kernel update if any
    if settings.upgrade.satellite_capsule_setup_reboot:
        reboot(180)
    host_ssh_availability_check(env.get('satellite_host'))
    # Test the Upgrade is successful
    upgrade_validation(True)
