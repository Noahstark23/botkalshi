from pathlib import Path
import re
import unittest


BASE = Path(__file__).resolve().parents[1]


class IntegrationUnitContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ai_service = (BASE / "botkalshi-assistant.service").read_text(encoding="utf-8")
        cls.ai_timer = (BASE / "botkalshi-assistant.timer").read_text(encoding="utf-8")
        cls.telegram_service = (BASE / "botkalshi-telegram.service").read_text(encoding="utf-8")
        cls.telegram_timer = (BASE / "botkalshi-telegram.timer").read_text(encoding="utf-8")
        cls.recovery_service = (BASE / "botkalshi-telegram-recovery.service").read_text(
            encoding="utf-8"
        )
        cls.failure_service = (BASE / "botkalshi-telegram-ai-failure.service").read_text(
            encoding="utf-8"
        )
        cls.notifier = (BASE / "telegram_notifier.py").read_text(encoding="utf-8")
        cls.installer = (BASE / "install-assistant-integrations.sh").read_text(encoding="utf-8")

    def test_services_use_distinct_unprivileged_identities_and_secret_files(self) -> None:
        self.assertIn("User=botkalshi-ai", self.ai_service)
        self.assertIn("Group=botkalshi-ai", self.ai_service)
        self.assertIn("EnvironmentFile=/etc/botkalshi-assistant.env", self.ai_service)
        self.assertIn(
            "LoadCredential=openai_api_key:/etc/botkalshi-assistant/openai_api_key",
            self.ai_service,
        )
        self.assertNotIn("/etc/botkalshi-research.env", _environment_file_lines(self.ai_service))
        self.assertNotIn("TELEGRAM_BOT_TOKEN=", self.ai_service)

        self.assertIn("User=botkalshi-telegram", self.telegram_service)
        self.assertIn("Group=botkalshi-telegram", self.telegram_service)
        self.assertNotIn("EnvironmentFile=", self.telegram_service)
        self.assertIn(
            "LoadCredential=telegram_bot_token:/etc/botkalshi-telegram/telegram_bot_token",
            self.telegram_service,
        )
        self.assertIn(
            "LoadCredential=telegram_chat_id:/etc/botkalshi-telegram/telegram_chat_id",
            self.telegram_service,
        )
        self.assertNotIn("/etc/botkalshi-research.env", _environment_file_lines(self.telegram_service))
        self.assertNotIn("OPENAI_API_KEY=", self.telegram_service)

    def test_services_are_non_executing_and_have_separate_write_scopes(self) -> None:
        for unit in (self.ai_service, self.telegram_service):
            self.assertIn("Environment=TRADING_ENABLED=false", unit)
            self.assertIn("Environment=MOTOR_MM_EXECUTION_ENABLED=false", unit)
            self.assertIn("CapabilityBoundingSet=\n", unit)
            self.assertIn("NoNewPrivileges=true", unit)
            self.assertIn("ProtectSystem=strict", unit)
            self.assertIn("ProtectProc=invisible", unit)
            self.assertIn("LimitCORE=0", unit)
            self.assertIn(
                "PYTHONOPTIMIZE PYTHONPATH PYTHONHOME PYTHONINSPECT",
                unit,
            )
            exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
            self.assertNotRegex(
                exec_start,
                r"(?i)place[-_ ]?order|cancel[-_ ]?order|move[-_ ]?money",
            )
        self.assertIn("ReadWritePaths=/var/lib/botkalshi-integrations/assistant\n", self.ai_service)
        self.assertIn(
            "InaccessiblePaths=-/etc/botkalshi-research.env -/etc/botkalshi-telegram "
            "-/var/lib/botkalshi-integrations/telegram",
            self.ai_service,
        )
        self.assertIn(
            "ReadWritePaths=/var/lib/botkalshi-integrations/telegram\n",
            self.telegram_service,
        )
        self.assertIn("--state-data /var/lib/botkalshi-integrations", self.ai_service)
        self.assertIn("--state-data /var/lib/botkalshi-integrations", self.telegram_service)
        self.assertIn("OnSuccess=botkalshi-telegram.service", self.ai_service)
        self.assertIn("OnFailure=botkalshi-telegram-ai-failure.service", self.ai_service)
        for path in (
            "health.json",
            "coverage.json",
            "risk-status.json",
            "packets/latest.json",
        ):
            self.assertIn(
                f"ReadWritePaths=-/var/lib/botkalshi-research/{path}",
                self.ai_service,
            )
            self.assertIn(
                f"ReadWritePaths=-/var/lib/botkalshi-research/{path}",
                self.telegram_service,
            )
            self.assertIn(
                f"ReadWritePaths=-/var/lib/botkalshi-research/{path}",
                self.recovery_service,
            )
        acl_comment = (
            "# Root ExecStartPre refreshes ACL xattrs; "
            "the service UID receives only r-- ACLs."
        )
        for unit in (self.ai_service, self.telegram_service, self.recovery_service):
            self.assertIn(acl_comment, unit)

    def test_timers_are_bounded_and_do_not_catch_up(self) -> None:
        self.assertIn("OnUnitInactiveSec=15min", self.ai_timer)
        self.assertIn("Persistent=false", self.ai_timer)
        self.assertIn("Unit=botkalshi-assistant.service", self.ai_timer)
        self.assertIn("OnUnitInactiveSec=5min", self.telegram_timer)
        self.assertIn("Persistent=false", self.telegram_timer)
        self.assertIn("Unit=botkalshi-telegram.service", self.telegram_timer)

    def test_recovery_and_ai_failure_units_have_fixed_bounded_events(self) -> None:
        self.assertIn("User=botkalshi-telegram", self.recovery_service)
        self.assertIn("SupplementaryGroups=botkalshi-ai", self.recovery_service)
        self.assertIn("--event recovery --force-confirm INSTALLATION_TEST", self.recovery_service)
        self.assertNotIn("[Install]", self.recovery_service)
        self.assertIn("User=botkalshi-telegram", self.failure_service)
        self.assertIn("--event ai-failure", self.failure_service)
        self.assertNotIn("[Install]", self.failure_service)
        self.assertNotIn("ExecStartPre=", self.failure_service)
        self.assertNotIn("/var/lib/botkalshi-research/health.json", self.failure_service)
        for unit in (self.telegram_service, self.recovery_service, self.failure_service):
            self.assertIn("TimeoutStartSec=60", unit)
        lock_wait = re.search(r"^LOCK_WAIT_SECONDS = ([0-9.]+)$", self.notifier, re.MULTILINE)
        http_timeout = re.search(
            r"^TELEGRAM_TIMEOUT_SECONDS = ([0-9.]+)$", self.notifier, re.MULTILINE
        )
        self.assertIsNotNone(lock_wait)
        self.assertIsNotNone(http_timeout)
        self.assertGreater(60, float(lock_wait.group(1)) + float(http_timeout.group(1)))

    def test_installer_never_controls_or_reconfigures_collector(self) -> None:
        forbidden = re.compile(
            r"systemctl\s+(?:restart|stop|disable|enable|start|try-restart|reload-or-restart)"
            r"[^\n]*botkalshi-research"
        )
        self.assertIsNone(forbidden.search(self.installer))
        self.assertNotIn("/etc/botkalshi-research.env\"", _install_destinations(self.installer))
        self.assertIn("COLLECTOR_PID_BEFORE", self.installer)
        self.assertIn("COLLECTOR_PID_AFTER", self.installer)
        self.assertIn("COLLECTOR_EXEC_BEFORE", self.installer)
        self.assertIn("COLLECTOR_EXEC_AFTER", self.installer)
        self.assertIn("verify_collector_identity", self.installer)
        self.assertIn("User --value)\" == 'botkalshi'", self.installer)
        self.assertIn("Group --value)\" == 'botkalshi'", self.installer)
        self.assertIn('/proc/{pid}/status', self.installer)

    def test_installer_stages_revision_and_hardens_secret_files(self) -> None:
        self.assertIn("git -C \"$STAGING\" fetch --depth 1 origin \"$SHA\"", self.installer)
        self.assertIn("mv \"$STAGING\" \"$RELEASE\"", self.installer)
        self.assertGreaterEqual(self.installer.count("-m 0600"), 2)
        self.assertIn("info.st_uid != 0", self.installer)
        self.assertIn("info.st_gid != 0", self.installer)
        self.assertIn("path.is_symlink()", self.installer)
        self.assertIn("read -r -s -p 'OPENAI_API_KEY", self.installer)
        self.assertIn("read -r -s -p 'TELEGRAM_BOT_TOKEN", self.installer)
        self.assertIn('install -d -o root -g root -m 0711 "$STATE"', self.installer)
        self.assertIn("/usr/libexec/botkalshi-prepare-integration-access", self.installer)
        self.assertIn("safe_release_git status --porcelain=v1", self.installer)
        self.assertIn("--ignored --ignore-submodules=all", self.installer)
        self.assertIn("-type f -links +1", self.installer)
        self.assertIn("/usr/bin/python3 -I -m unittest discover", self.installer)
        self.assertIn("Collector, IA y Telegram requieren UIDs distintos", self.installer)
        self.assertIn("Un UID de servicio está compartido con otra identidad", self.installer)
        self.assertIn("Las identidades y grupos de servicio nunca pueden ser root", self.installer)
        self.assertIn("Collector, IA y Telegram requieren GIDs primarios distintos", self.installer)
        self.assertIn("Un GID de servicio está compartido con otro grupo", self.installer)
        self.assertIn("os.getgrouplist", self.installer)
        self.assertIn("El grupo compartido IA contiene identidades no autorizadas", self.installer)
        self.assertIn("updated_by\") == \"fail-closed-default", self.installer)
        self.assertIn("apt-get install -y -qq --no-install-recommends --no-upgrade", self.installer)
        self.assertIn("export NEEDRESTART_MODE=l", self.installer)
        self.assertIn("El PID del collector cambió durante prerrequisitos", self.installer)
        self.assertNotIn("apt-get install -y -qq git ca-certificates python3 acl", self.installer)
        self.assertIn("/usr/bin/flock --exclusive --nonblock 9", self.installer)
        self.assertIn(
            "unset PYTHONOPTIMIZE PYTHONPATH PYTHONHOME PYTHONINSPECT",
            self.installer,
        )
        self.assertTrue(self.installer.startswith("#!/bin/bash\n"))
        self.assertIn("export PATH='/usr/sbin:/usr/bin:/sbin:/bin'", self.installer)
        self.assertNotRegex(self.installer, r"(?m)^\s*assert\s+")
        helper = (BASE / "prepare-integration-access.sh").read_text(encoding="utf-8")
        self.assertTrue(helper.startswith("#!/bin/bash\n"))
        self.assertIn("export PATH='/usr/sbin:/usr/bin:/sbin:/bin'", helper)
        self.assertIn("os.O_NOFOLLOW", helper)
        self.assertIn("pass_fds=(fd,)", helper)
        self.assertIn('f"/proc/self/fd/{fd}"', helper)
        self.assertIn('packets_fd = open_directory("packets", dir_fd=data_fd)', helper)
        self.assertIn("info.st_uid != collector_uid", helper)
        self.assertIn('mode = sys.argv[2]', helper)
        self.assertIn('bootstrap = mode == "bootstrap"', helper)
        self.assertIn('if bootstrap:\n        grant(data_fd, "--x")', helper)
        self.assertIn('if packets_fd is not None and bootstrap:', helper)
        self.assertNotIn("d:u:", helper)

    def test_upgrade_quiesces_integration_state_and_proves_fresh_assessment(self) -> None:
        quiesce = self.installer.index(
            "systemctl stop botkalshi-assistant.timer botkalshi-telegram.timer"
        )
        state_setup = self.installer.index(
            'install -d -o root -g root -m 0711 "$STATE"'
        )
        self.assertLess(quiesce, state_setup)
        self.assertIn("os.O_NOFOLLOW", self.installer)
        self.assertIn("os.fchown(file_fd, uid, gid)", self.installer)
        self.assertIn("ASSESSMENT_ID_BEFORE", self.installer)
        self.assertIn("SMOKE_STARTED_AT", self.installer)
        self.assertIn("La evaluación inicial no es fresca", self.installer)

    def test_timers_enable_only_after_both_initial_runs(self) -> None:
        ai_start = self.installer.index("systemctl start botkalshi-assistant.service")
        telegram_start = self.installer.index("systemctl start botkalshi-telegram.service")
        enable = self.installer.index(
            "systemctl enable botkalshi-assistant.timer botkalshi-telegram.timer"
        )
        self.assertLess(ai_start, telegram_start)
        self.assertLess(telegram_start, enable)
        self.assertIn("TELEGRAM_MESSAGE_AFTER", self.installer)
        self.assertIn("No se verificó una entrega Telegram nueva", self.installer)
        schema_match = re.search(r'^STATE_SCHEMA = "([^"]+)"$', self.notifier, re.MULTILINE)
        self.assertIsNotNone(schema_match)
        self.assertIn(schema_match.group(1), self.installer)


def _environment_file_lines(unit: str) -> str:
    return "\n".join(line for line in unit.splitlines() if line.startswith("EnvironmentFile="))


def _install_destinations(script: str) -> str:
    return "\n".join(line for line in script.splitlines() if line.lstrip().startswith("install "))


if __name__ == "__main__":
    unittest.main()
