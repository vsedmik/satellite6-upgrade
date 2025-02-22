import sys

from automation_tools import setup_capsule_firewall
from fabric.api import execute
from fabric.api import run
from fabric.api import settings as fabric_settings
from robozilla.decorators import bz_bug_is_open

from upgrade.helpers import settings
from upgrade.helpers.constants.constants import RHEL_CONTENTS
from upgrade.helpers.logger import logger
from upgrade.helpers.tasks import add_baseOS_repo
from upgrade.helpers.tasks import capsule_sync
from upgrade.helpers.tasks import enable_disable_repo
from upgrade.helpers.tasks import foreman_maintain_package_update
from upgrade.helpers.tasks import foreman_service_restart
from upgrade.helpers.tasks import http_proxy_config
from upgrade.helpers.tasks import nonfm_upgrade
from upgrade.helpers.tasks import sync_capsule_repos_to_satellite
from upgrade.helpers.tasks import update_capsules_to_satellite
from upgrade.helpers.tasks import upgrade_using_foreman_maintain
from upgrade.helpers.tasks import upgrade_validation
from upgrade.helpers.tasks import wait_untill_capsule_sync
from upgrade.helpers.tasks import workaround_1829115
from upgrade.helpers.tasks import yum_repos_cleanup
from upgrade.helpers.tools import copy_ssh_key
from upgrade.helpers.tools import host_pings
from upgrade.helpers.tools import host_ssh_availability_check
from upgrade.helpers.tools import reboot

logger = logger()


def satellite_capsule_setup(satellite_host, capsule_hosts, os_version,
                            upgradable_capsule=True):
    """
    Setup all pre-requisites for user provided capsule

    :param satellite_host: Satellite hostname to which the capsule registered
    :param capsule_hosts: List of capsule which mapped with satellite host
    :param os_version: The OS version onto which the capsule installed e.g: rhel6, rhel7
    :param upgradable_capsule:Whether to setup capsule to be able to upgrade in future
    :return: capsule_hosts
    """
    if os_version == 'rhel6':
        baseurl = settings.repos.rhel6_os
    elif os_version == 'rhel7':
        baseurl = settings.repos.rhel7_os
    else:
        logger.warning('No OS Specified. Terminating..')
        sys.exit(1)
    non_responsive_host = []
    for cap_host in capsule_hosts:
        if not host_pings(cap_host):
            non_responsive_host.append(cap_host)
        else:
            execute(host_ssh_availability_check, cap_host)
        # Update the template once 1829115 gets fixed.
        execute(workaround_1829115, host=cap_host)
        if not bz_bug_is_open(1829115):
            logger.warn("Please update the capsule template for fixed capsule version")
        execute(foreman_service_restart, host=cap_host)
        if non_responsive_host:
            logger.warning(str(non_responsive_host) + ' these are '
                                                      'non-responsive hosts')
            sys.exit(1)
        copy_ssh_key(satellite_host, capsule_hosts)
    if upgradable_capsule:
        if settings.upgrade.distribution == "cdn":
            settings.repos.capsule_repo = None
            settings.repos.sattools_repo[settings.upgrade.os] = None
            settings.repos.satmaintenance_repo = None
        execute(update_capsules_to_satellite, capsule_hosts, host=satellite_host)
        if settings.upgrade.upgrade_with_http_proxy:
            http_proxy_config(capsule_hosts)
        execute(sync_capsule_repos_to_satellite, capsule_hosts, host=satellite_host)
        for cap_host in capsule_hosts:
            settings.upgrade.capsule_hostname = cap_host
            execute(add_baseOS_repo, baseurl, host=cap_host)
            execute(yum_repos_cleanup, host=cap_host)
            logger.info(f'Capsule {cap_host} is ready for Upgrade')
        return capsule_hosts


def satellite_capsule_upgrade(cap_host, sat_host):
    """Upgrades capsule from existing version to latest version.

    :param string cap_host: Capsule hostname onto which the capsule upgrade
    will run
    :param string sat_host : Satellite hostname from which capsule certs are to
    be generated

    The following environment variables affect this command:

    CAPSULE_URL
        Optional, defaults to available capsule version in CDN.
        URL for capsule of latest compose to upgrade.
    FROM_VERSION
        Capsule current version, to disable repos while upgrading.
        e.g '6.1','6.0'
    TO_VERSION
        Capsule version to upgrade to and enable repos while upgrading.
        e.g '6.1','6.2'

    """
    logger.highlight('\n========== CAPSULE UPGRADE =================\n')
    # Check the capsule sync before upgrade.
    logger.info("Check the capsule sync after satellite upgrade to verify sync operation "
                "with n-1 combination")
    execute(capsule_sync, cap_host, host=sat_host)
    wait_untill_capsule_sync(cap_host)
    from_version = settings.upgrade.from_version
    to_version = settings.upgrade.to_version
    setup_capsule_firewall()
    major_ver = settings.upgrade.os[-1]
    ak_name = settings.upgrade.capsule_ak[settings.upgrade.os]
    run(f'subscription-manager register --org="Default_Organization" '
        f'--activationkey={ak_name} --force')
    logger.info(f"Activation key {ak_name} registered capsule's all available repository")
    run("subscription-manager repos --list")
    maintenance_repo = [RHEL_CONTENTS["maintenance"]["label"]]
    capsule_repos = [
        RHEL_CONTENTS["tools"]["label"],
        RHEL_CONTENTS["capsule"]["label"],
    ]
    with fabric_settings(warn_only=True):
        if settings.upgrade.distribution == "cdn":
            enable_disable_repo(enable_repos_name=capsule_repos + maintenance_repo)
        else:
            enable_disable_repo(disable_repos_name=maintenance_repo)

    if from_version != to_version:
        with fabric_settings(warn_only=True):
            enable_disable_repo(disable_repos_name=capsule_repos)
    with fabric_settings(warn_only=True):
        enable_disable_repo(enable_repos_name=[
            f"rhel-{major_ver}-server-ansible-{settings.upgrade.ansible_repo_version}-rpms"])

    if settings.upgrade.foreman_maintain_capsule_upgrade:
        foreman_maintain_package_update()
        upgrade_using_foreman_maintain(sat_host=False)
    else:
        nonfm_upgrade(satellite_upgrade=False,
                      cap_host=cap_host,
                      sat_host=sat_host)
    # Rebooting the capsule for kernel update if any
    reboot(160)
    host_ssh_availability_check(cap_host)
    # Check if Capsule upgrade is success
    upgrade_validation()
    # Check the capsule sync after upgrade.
    logger.info("check the capsule sync after capsule upgrade")
    execute(capsule_sync, cap_host, host=sat_host)
    wait_untill_capsule_sync(cap_host)


def satellite_capsule_zstream_upgrade(cap_host):
    """Upgrades Capsule to its latest zStream version

    :param string cap_host: Capsule hostname onto which the capsule upgrade
    will run

    Note: For zstream upgrade both 'To' and 'From' version should be same

    FROM_VERSION
        Current satellite version which will be upgraded to latest version
    TO_VERSION
        Next satellite version to which satellite will be upgraded
    """
    logger.highlight('\n========== CAPSULE UPGRADE =================\n')
    from_version = settings.upgrade.from_version
    to_version = settings.upgrade.to_version
    if not from_version == to_version:
        logger.warning('zStream Upgrade on Capsule cannot be performed as '
                       'FROM and TO versions are not same!')
        sys.exit(1)
    major_ver = settings.upgrade.os[-1]
    ak_name = settings.upgrade.capsule_ak[settings.upgrade.os]
    run(f'subscription-manager register --org="Default_Organization" '
        f'--activationkey={ak_name} --force')
    logger.info(f"Activation key {ak_name} registered capsule's all available repository")
    run("subscription-manager repos --list")
    capsule_repos = [
        RHEL_CONTENTS["tools"]["label"],
        RHEL_CONTENTS["capsule"]["label"],
        RHEL_CONTENTS["maintenance"]["label"]
    ]
    with fabric_settings(warn_only=True):
        if settings.upgrade.distribution == "cdn":
            enable_disable_repo(enable_repos_name=capsule_repos)
        else:
            enable_disable_repo(disable_repos_name=capsule_repos)
        ansible_repos = [f"rhel-{major_ver}-server-ansible-"
                         f"{settings.upgrade.ansible_repo_version}-rpms"]
        enable_disable_repo(enable_repos_name=ansible_repos)
    # Check what repos are set
    # setup_foreman_maintain_repo()
    if settings.upgrade.foreman_maintain_capsule_upgrade:
        upgrade_using_foreman_maintain(sat_host=False)
    else:
        nonfm_upgrade(satellite_upgrade=False)
    # Rebooting the capsule for kernel update if any
    if settings.upgrade.satellite_capsule_setup_reboot:
        reboot(160)
    host_ssh_availability_check(cap_host)
    # Check if Capsule upgrade is success
    upgrade_validation()
