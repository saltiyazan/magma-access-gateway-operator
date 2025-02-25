#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

"""Machine Charm for Magma's Access Gateway."""

import ipaddress
import json
import logging
import re
import subprocess
from ipaddress import AddressValueError
from pathlib import Path
from typing import List, Optional, Tuple

import netifaces  # type: ignore[import]
from charms.lte_core_interface.v0.lte_core_interface import LTECoreProvides
from charms.magma_orchestrator_interface.v0.magma_orchestrator_interface import (
    OrchestratorAvailableEvent,
    OrchestratorRequires,
)
from ops.charm import (
    ActionEvent,
    CharmBase,
    InstallEvent,
    RelationJoinedEvent,
    StartEvent,
)
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

logger = logging.getLogger(__name__)

ROOT_CA_PATH = "/var/opt/magma/tmp/certs/rootCA.pem"
CERT_CERTIFIER_CERT = "/var/opt/magma/tmp/certs/certifier.pem"
CONFIG_PATH = "/var/opt/magma/configs/control_proxy.yml"


def install_file(file: Path, content: str) -> bool:
    """Install file with provided text content.

    Args:
        file: Path object to write to
        content: Text content to write to the file

    Returns:
        True if the file was written to
    """
    if not file.parent.exists():
        file.parent.mkdir()
    elif file.exists() and file.read_text() == content:
        return False
    file.write_text(content)
    return True


class MagmaAccessGatewayOperatorCharm(CharmBase):
    """Charm the service."""

    HARDWARE_ID_LABEL = "Hardware ID"
    CHALLENGE_KEY_LABEL = "Challenge key"

    def __init__(self, *args):
        """Observes juju events."""
        super().__init__(*args)
        self._lte_core_provides = LTECoreProvides(self, "lte-core")
        self.orchestrator_requirer = OrchestratorRequires(self, "magma-orchestrator")
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.config_changed, self._on_install)

        self.framework.observe(
            self.on.get_access_gateway_secrets_action, self._on_get_access_gateway_secrets
        )
        self.framework.observe(
            self.on.post_install_checks_action, self._on_post_install_checks_action
        )

        self.framework.observe(
            self.orchestrator_requirer.on.orchestrator_available,
            self._on_orchestrator_available,
        )

        self.framework.observe(
            self.on["lte-core"].relation_joined, self._on_lte_core_relation_joined
        )

    def _on_install(self, event: InstallEvent) -> None:
        """Triggered on install event.

        Args:
            event: Juju event

        Returns:
            None
        """
        if self._is_magmad_enabled:
            return
        self.unit.status = MaintenanceStatus("Installing AGW Snap")
        self.install_magma_access_gateway_snap()
        if not self._is_configuration_valid:
            self.unit.status = BlockedStatus("Configuration is invalid. Check logs for details")
            return
        self.unit.status = MaintenanceStatus("Installing AGW")
        returncode = self.install_magma_access_gateway()
        if returncode != 0:
            self.unit.status = BlockedStatus("Installation script failed. See logs for details")
            return
        self.unit.status = MaintenanceStatus("Rebooting to apply changes")
        self.reboot()

    def _on_start(self, event: StartEvent):
        """Triggered on start event.

        Args:
            event: Juju event

        Returns:
            None
        """
        if not self._magma_service_is_running:
            event.defer()
            return
        self.unit.status = ActiveStatus()

    def _on_get_access_gateway_secrets(self, event: ActionEvent) -> None:
        """Triggered on get-access-gateway-secrets action call.

        Returns Access Gateway's Hardware ID and Challenge Key required to integrate AGW with
        the Orchestrator.
        """
        if not self._magma_service_is_running:
            event.fail("Magma is not running! Please start Magma and try again.")
            return
        try:
            hardware_id, challenge_key = self._get_magma_secrets
            event.set_results(
                {
                    "hardware-id": hardware_id,
                    "challenge-key": challenge_key,
                }
            )
        except (subprocess.CalledProcessError, IndexError, ValueError):
            event.fail("Failed to get Magma Access Gateway secrets!")
            return
        except Exception as e:
            event.fail(str(e))
            return

    def _on_post_install_checks_action(self, event: ActionEvent) -> None:
        """Triggered on post install checks action.

        Args:
            event: Juju event (ActionEvent)

        Returns:
            None
        """
        command = ["magma-access-gateway.post-install"]
        successful_msg = "Magma AGW post-installation checks finished successfully."
        failed_msg = "Post-installation checks failed. For more information, please check journalctl logs."  # noqa: E501
        try:
            post_install_checks = subprocess.run(command, stdout=subprocess.PIPE)
            event.set_results(
                {
                    "post-install-checks-output": successful_msg
                    if (post_install_checks.returncode == 0)
                    else failed_msg
                }
            )
        except subprocess.CalledProcessError:
            event.fail("Failed to run post-install checks.")
            return
        except Exception as e:
            event.fail(str(e))
            return

    def _on_orchestrator_available(self, event: OrchestratorAvailableEvent):
        """Triggered when a related orchestrator is made available.

        The AGW will be configured to connect to the orchestrator with the data from
        the event. Services will then be restarted.
        """
        if self._certifier_pem_changed(event.certifier_pem_certificate):
            self._remove_agw_cert_files()
        if self._install_configurations(event):
            self.unit.status = MaintenanceStatus("Restarting Access Gateway to apply changes")
            self._restart_magma()
        if not self._magma_service_is_running:
            event.defer()
            return
        self.unit.status = ActiveStatus()

    def _on_lte_core_relation_joined(self, event: RelationJoinedEvent):
        """Triggered when lte-core relation is joined.

        AGW will provide the IP address of the MME (eth1) interface if that is available.

        Args:
            event: Juju event (RelationJoinedEvent)

        Returns:
            None
        """
        if not self.unit.is_leader():
            return
        try:
            ip = netifaces.ifaddresses("eth1")[netifaces.AF_INET][0]["addr"]
            self._lte_core_provides.set_lte_core_information(ip)
            self.unit.status = ActiveStatus()
        except (ValueError, AddressValueError) as e:
            logger.error(f"Failed to fetch IP address of eth1 interface: {str(e)}")
            self.unit.status = WaitingStatus("Waiting for the MME interface to be ready")
            event.defer()
            return

    @staticmethod
    def install_magma_access_gateway_snap() -> None:
        """Installs Magma Access Gateway snap.

        Returns:
            None
        """
        subprocess.run(
            ["snap", "install", "magma-access-gateway", "--classic", "--edge"],
            stdout=subprocess.PIPE,
        )

    def install_magma_access_gateway(self) -> int:
        """Installs Magma access gateway on the host.

        Returns:
            Return code of the installation script
        """
        command = ["magma-access-gateway.install"]
        command.extend(self._install_arguments)
        install_process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
        )
        logger.info(install_process.stdout)
        return install_process.returncode

    def reboot(self) -> None:
        """Sends the command to reboot the machine in 1 minute."""
        subprocess.run(
            ["shutdown", "--reboot", "+1"],
            stdout=subprocess.PIPE,
        )

    @property
    def _is_configuration_valid(self) -> bool:
        """Validates configuration."""
        if self.model.config["skip-networking"]:
            return True
        valid = self._is_valid_interface("sgi", "eth0")
        if not self._is_valid_interface("s1", "eth1"):
            valid = False
        if not self._is_valid_sgi_interface_addressing_configuration:
            valid = False
        if not self._is_valid_s1_interface_addressing_configuration:
            valid = False
        if not self._are_valid_dns(self.model.config["dns"]):
            logger.warning("Invalid DNS configuration")
            valid = False
        return valid

    def _is_valid_interface(self, interface_name: str, new_interface_name: str) -> bool:
        """Validates a network interface name.

        An interface name is required and must represent an interface present on the
        machine. Because Magma requires interfaces to be named a certain way, the
        installation will rename the interfaces. For that reason, we also check for
        the renamed interface to be present.

        Args:
            interface_name: Original name of the interface
            new_interface_name: Name of the interface that will be set by Magma

        Returns:
            True if the interface name is valid and found
        """
        interface = self.model.config.get(interface_name)
        if not interface:
            logger.warning("%s interface name is required", (interface_name))
            return False
        if (
            interface not in netifaces.interfaces()
            and new_interface_name not in netifaces.interfaces()  # noqa: W503
        ):
            logger.warning("%s interface not found", (interface))
            return False
        return True

    def _certifier_pem_changed(self, new_cert):
        return (
            Path(CERT_CERTIFIER_CERT).exists()
            and Path(CERT_CERTIFIER_CERT).read_text() != new_cert  # noqa: W503
        )

    @staticmethod
    def _remove_agw_cert_files():
        for file in [
            "/var/opt/magma/gateway.crt",
            "/var/opt/magma/gateway.key",
            "/var/opt/magma/gw_challenge.key",
        ]:
            try:
                Path(file).unlink()
            except FileNotFoundError:
                logger.debug("File does not exist: " + file)

    @property
    def _is_valid_sgi_interface_addressing_configuration(self) -> bool:
        """Validates sgi interface configuration."""
        ipv4_address = self.model.config.get("sgi-ipv4-address")
        ipv4_gateway = self.model.config.get("sgi-ipv4-gateway")
        ipv6_address = self.model.config.get("sgi-ipv6-address")
        ipv6_gateway = self.model.config.get("sgi-ipv6-gateway")
        if not ipv4_address and not ipv4_gateway and not ipv6_address and not ipv6_gateway:
            return True
        if any([ipv4_address, ipv4_gateway]) and not all([ipv4_address, ipv4_gateway]):
            logger.warning("Both IPv4 address and gateway required for interface sgi")
            return False
        if any([ipv6_address, ipv6_gateway]) and not all([ipv6_address, ipv6_gateway]):
            logger.warning("Both IPv6 address and gateway required for interface sgi")
            return False
        if ipv6_address and not ipv4_address:
            logger.warning("Pure IPv6 configuration is not supported for interface sgi")
            return False
        if ipv4_address and not self._is_valid_ipv4_address(ipv4_address):
            logger.warning("Invalid IPv4 address and netmask for interface sgi")
            return False
        if ipv4_gateway and not self._is_valid_ipv4_gateway(ipv4_gateway):
            logger.warning("Invalid IPv4 gateway for interface sgi")
            return False
        if ipv6_address and not self._is_valid_ipv6_address(ipv6_address):
            logger.warning("Invalid IPv6 address and netmask for interface sgi")
            return False
        if ipv6_gateway and not self._is_valid_ipv6_gateway(ipv6_gateway):
            logger.warning("Invalid IPv6 gateway for interface sgi")
            return False
        return True

    @property
    def _is_valid_s1_interface_addressing_configuration(self) -> bool:
        """Validates s1 interface configuration."""
        ipv4_address = self.model.config.get("s1-ipv4-address")
        ipv6_address = self.model.config.get("s1-ipv6-address")
        if not ipv4_address and not ipv6_address:
            return True
        if ipv6_address and not ipv4_address:
            logger.warning("Pure IPv6 configuration is not supported for interface s1")
            return False
        if ipv4_address and not self._is_valid_ipv4_address(ipv4_address):
            logger.warning("Invalid IPv4 address and netmask for interface s1")
            return False
        if ipv6_address and not self._is_valid_ipv6_address(ipv6_address):
            logger.warning("Invalid IPv6 address and netmask for interface s1")
            return False
        return True

    @staticmethod
    def _is_valid_ipv4_address(ipv4_address: str) -> bool:
        """Validate an IPv4 address and netmask.

        A valid string will have the form:
        a.b.c.d/x
        """
        try:
            ip = ipaddress.ip_network(ipv4_address, strict=False)
            return isinstance(ip, ipaddress.IPv4Network) and ip.prefixlen != 32
        except ValueError:
            return False

    @staticmethod
    def _is_valid_ipv4_gateway(ipv4_gateway: str) -> bool:
        """Validate an IPv4 gateway.

        A valid string will have the form:
        a.b.c.d
        """
        try:
            ip = ipaddress.ip_address(ipv4_gateway)
            return isinstance(ip, ipaddress.IPv4Address)
        except ValueError:
            return False

    @staticmethod
    def _is_valid_ipv6_address(ipv6_address: str) -> bool:
        """Validate an IPv6 address and netmask.

        A valid string will contain an IPv6 address followed by a
        netmask, like this:
        2001:0db8:85a3:0000:0000:8a2e:0370:7334/64
        """
        try:
            ip = ipaddress.ip_network(ipv6_address, strict=False)
            return isinstance(ip, ipaddress.IPv6Network) and ip.prefixlen != 128
        except ValueError:
            return False

    @staticmethod
    def _is_valid_ipv6_gateway(ipv6_gateway: str) -> bool:
        """Validate an IPv6 gateway.

        A valid string will contain an IPv6 address, like this:
        2001:0db8:85a3:0000:0000:8a2e:0370:7334
        """
        try:
            ip = ipaddress.ip_address(ipv6_gateway)
            return isinstance(ip, ipaddress.IPv6Address)
        except ValueError:
            return False

    @staticmethod
    def _are_valid_dns(dns: str) -> bool:
        """Validate that provided string is a list of IP addresses."""
        try:
            list_of_dns = json.loads(dns)
            if not isinstance(list_of_dns, list) or not list_of_dns:
                return False
            try:
                [ipaddress.ip_address(dns) for dns in list_of_dns]
            except ValueError:
                return False
        except json.JSONDecodeError:
            return False
        return True

    @property
    def _install_arguments(self) -> List[str]:
        """Prepares argument list for install command from configuration.

        Returns:
            List of arguments for install command
        """
        config = dict(self.model.config)
        if config.pop("skip-networking"):
            return ["--no-reboot", "--skip-networking"]
        arguments = ["--no-reboot", "--dns"]
        arguments.extend(json.loads(config.pop("dns")))
        for key, value in config.items():
            arguments.extend([f"--{key}", value])
        return arguments

    @property
    def _magma_service_is_running(self) -> bool:
        """Checks whether magma is running."""
        magma_service = subprocess.run(
            ["systemctl", "is-active", "magma@magmad"],
            stdout=subprocess.PIPE,
        )
        return magma_service.returncode == 0

    @property
    def _get_magma_secrets(self) -> Tuple[Optional[str], Optional[str]]:
        """Gets Access Gateway's Hardware ID and Challenge key.

        Hardware ID and Challenge key are available through the `show_gateway_info` script
        provided by the Access Gateway. This method filers out required values from the script's
        output.

        Returns:
            str: Hardware ID
            str: Challenge key
        """
        gateway_info = subprocess.check_output(["show_gateway_info.py"]).decode().split("\n")
        gateway_info = list(filter(None, gateway_info))
        gateway_info = list(filter(lambda x: (not re.search("^-(-*)", x)), gateway_info))
        hardware_id = gateway_info[gateway_info.index(self.HARDWARE_ID_LABEL) + 1]
        challenge_key = gateway_info[gateway_info.index(self.CHALLENGE_KEY_LABEL) + 1]
        return hardware_id, challenge_key

    @property
    def _is_magmad_enabled(self) -> bool:
        """Validates if magmad service is enabled."""
        magma_service = subprocess.run(
            ["systemctl", "is-enabled", "magma@magmad"],
            stdout=subprocess.PIPE,
        )
        return not magma_service.returncode

    def _install_configurations(self, event: OrchestratorAvailableEvent) -> bool:
        """Install or update configuration files.

        Returns:
            True if any changes were applied
        """
        config = self._generate_config(
            orchestrator_address=event.orchestrator_address,
            orchestrator_port=event.orchestrator_port,
            bootstrapper_address=event.bootstrapper_address,
            bootstrapper_port=event.bootstrapper_port,
            fluentd_address=event.fluentd_address,
            fluentd_port=event.fluentd_port,
        )
        return any(
            [
                install_file(Path(ROOT_CA_PATH), event.root_ca_certificate),
                install_file(Path(CERT_CERTIFIER_CERT), event.certifier_pem_certificate),
                install_file(Path(CONFIG_PATH), config),
            ]
        )

    @staticmethod
    def _generate_config(
        orchestrator_address: str,
        orchestrator_port: int,
        bootstrapper_address: str,
        bootstrapper_port: int,
        fluentd_address: str,
        fluentd_port: int,
    ) -> str:
        return (
            f"cloud_address: {orchestrator_address}\n"
            f"cloud_port: {orchestrator_port}\n"
            f"bootstrap_address: {bootstrapper_address}\n"
            f"bootstrap_port: {bootstrapper_port}\n"
            f"fluentd_address: {fluentd_address}\n"
            f"fluentd_port: {fluentd_port}\n"
            "\n"
            f"rootca_cert: {ROOT_CA_PATH}\n"
        )

    @staticmethod
    def _restart_magma() -> None:
        subprocess.run(
            ["service", "magma@*", "stop"],
            stdout=subprocess.PIPE,
        )
        subprocess.run(
            ["service", "magma@magmad", "start"],
            stdout=subprocess.PIPE,
        )


if __name__ == "__main__":
    main(MagmaAccessGatewayOperatorCharm)
