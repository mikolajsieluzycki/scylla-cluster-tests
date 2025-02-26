#!/usr/bin/env python3
import logging
import sys
import re
import os

from pkg_resources import parse_version
from sdcm.utils.version_utils import is_enterprise, get_all_versions
from sdcm.utils.common import get_s3_scylla_repos_mapping

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))


LOGGER = logging.getLogger(__name__)


# We support to migrate from specific OSS version to enterprise
supported_src_oss = {'2022.1': '4.6', '2021.1': '4.3', '2020.1': '4.0', '2019.1': '3.0'}
# If new support distro shared repo with others, we need to assign the start support versions. eg: centos8
start_support_versions = {'centos-8': {'scylla': '4.1', 'enterprise': '2021.1'}}


class UpgradeBaseVersion:  # pylint: disable=too-many-instance-attributes

    oss_start_support_version = None
    ent_start_support_version = None

    def __init__(self, scylla_repo: str, linux_distro: str, scylla_version: str = None):
        if len(linux_distro.split('-')) > 1:
            self.dist_type, *_, self.dist_version = linux_distro.split('-')
        else:
            self.dist_type = linux_distro.split('-')[0]
            self.dist_version = None
        self.scylla_repo = scylla_repo
        self.linux_distro = linux_distro
        self.product, self.scylla_version = self.get_product_and_version(scylla_version)
        self.repo_maps = get_s3_scylla_repos_mapping(self.dist_type, self.dist_version)

    def get_product_and_version(self, scylla_version: str = None) -> tuple[str, str]:
        """
        return scylla product name and major version. if scylla_version isn't assigned,
        we will try to get the major version from the scylla_repo url.
        """
        LOGGER.info("Getting scylla product and major version for upgrade versions listing...")
        if scylla_version is None:
            assert 'unstable/' in self.scylla_repo, "Did not find 'unstable/' in scylla_repo. " \
                                                    "Scylla repo: %s" % self.scylla_repo
            product, version = self.scylla_repo.split('unstable/')[1].split('/')[0:2]
            scylla_version = version.replace('branch-', '').replace('enterprise-', '')
        else:
            product = 'scylla-enterprise' if is_enterprise(scylla_version) else 'scylla'
        LOGGER.info("Scylla product and major version used for upgrade versions listing: %s, %s", product, version)
        return product, scylla_version

    def set_start_support_version(self) -> None:
        """
        We can't detect the support versions for this distro, which shares the repo with others, eg: centos8
        so we need to assign the start support versions for it.
        """
        LOGGER.info("Setting start support version...")
        if self.dist_type not in ['debian', 'ubuntu'] and self.linux_distro != 'centos':
            versions_dict = start_support_versions.get(self.linux_distro, None)
            if versions_dict is None:
                raise Exception(
                    f"We can't detect the support versions for this distro, which shares the repo with {self.dist_type}.")
            self.oss_start_support_version = versions_dict['scylla']
            self.ent_start_support_version = versions_dict['enterprise']
            LOGGER.info("Support start versions set: oss=%s enterprise=%s", self.oss_start_support_version,
                        self.ent_start_support_version)

    def get_supported_scylla_base_versions(self, supported_versions) -> list:  # pylint: disable=too-many-branches
        """
        We have special base versions list for each release, and we don't support to upgraded from enterprise
        to opensource. This function is used to get the base versions list which will be used in the upgrade test.

        @supported_version: all scylla release versions, the base versions will be filtered out from the supported_version
        """
        LOGGER.info("Getting supported scylla base versions...")
        oss_base_version = []
        ent_base_version = []

        oss_release_list = [v for v in supported_versions if not is_enterprise(v)]
        ent_release_list = [v for v in supported_versions if is_enterprise(v)]

        # The major version of unstable scylla, eg: 4.6.dev, 4.5, 2021.2.dev, 2021.1
        version = self.scylla_version
        product = self.product

        if product == 'scylla-enterprise':
            if version in supported_versions:
                # The dest version is a released enterprise version
                idx = ent_release_list.index(version)
                oss_base_version.append(supported_src_oss.get(version))
                if idx == 0:
                    # The dest version is a minor release of latest enterprise release
                    ent_base_version.append(version)
                else:
                    # Choose the last two releases as upgrade base
                    ent_base_version += ent_release_list[idx-1:][:2]
            elif version == 'enterprise' or parse_version(version) > parse_version(ent_release_list[0]):
                ent_base_version.append(ent_release_list[-1])
            elif re.match(r'\d+.\d+', version) and parse_version(version) >= parse_version(ent_release_list[0]):
                oss_base_version.append(oss_release_list[-1])
                ent_base_version += ent_release_list[-2:]
        elif product == 'scylla':
            if version in supported_versions:
                # The dest version is a released opensource version
                idx = oss_release_list.index(version)
                if idx == 0:
                    # The dest version is a minor release of latest enterprise release
                    oss_base_version.append(version)
                else:
                    # Choose the last two releases as upgrade base
                    oss_base_version += oss_release_list[idx-1:][:2]
            elif version == 'master' or parse_version(version) > parse_version(oss_release_list[0]):
                oss_base_version.append(oss_release_list[-1])
            elif re.match(r'\d+.\d+', version) and parse_version(version) < parse_version(oss_release_list[0]):
                # If dest version is smaller than the first supported opensource release, it might be an invalid dest version
                oss_base_version.append(oss_release_list[-1])
        else:
            raise ValueError("Unsupported product %s" % product)

        # Filter out unsupported version
        oss_base_version = [v for v in oss_base_version if v in supported_versions]
        ent_base_version = [v for v in ent_base_version if v in supported_versions]

        # if there's only release candidates in those repos, skip this version
        oss_base_version = self.filter_rc_only_version(oss_base_version)
        ent_base_version = self.filter_rc_only_version(ent_base_version)
        LOGGER.info("Supported scylla base versions for: oss=%s enterprise=%s", oss_base_version, ent_base_version)
        return oss_base_version + ent_base_version

    def filter_rc_only_version(self, base_version_list: list) -> list:
        LOGGER.info("Filtering rc versions from base version list...")
        if base_version_list and len(base_version_list) > 1:
            # if there's only release candidates in this repo, skip this version
            filter_rc = [v for v in get_all_versions(self.repo_maps[base_version_list[-1]]) if 'rc' not in v]
            if not filter_rc:
                base_version_list = base_version_list[:-1]
        return base_version_list

    def get_version_list(self) -> tuple[list, list]:
        """
        return all supported releases versions, and the supported base versions of upgrade test.
        """
        LOGGER.info("Getting the supported releases version list...")
        supported_versions = []

        # Filter out the unsupported versions
        for version_prefix, _ in self.repo_maps.items():
            # can't find the major version from the version_prefix string
            if not re.match(r'\d+.\d+', version_prefix):
                continue
            # OSS: the major version is smaller than the start support version
            if self.oss_start_support_version and not is_enterprise(version_prefix) and \
                    parse_version(version_prefix) < parse_version(self.oss_start_support_version):
                continue
            # Enterprise: the major version is smaller than the start support version
            if self.ent_start_support_version and is_enterprise(version_prefix) and \
                    parse_version(version_prefix) < parse_version(self.ent_start_support_version):
                continue
            supported_versions.append(version_prefix)
        version_list = self.get_supported_scylla_base_versions(supported_versions)
        LOGGER.info("Got supported releases version list: %s", version_list)
        return supported_versions, version_list
