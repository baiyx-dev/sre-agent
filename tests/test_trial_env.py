import tempfile
import unittest
from pathlib import Path

from scripts.generate_trial_env import (
    generate_trial_environment,
    read_trial_environment,
    validate_upgrade_contact_url,
    write_trial_environment,
)


class TrialEnvironmentTests(unittest.TestCase):
    def test_generates_strong_unique_local_trial_secrets(self):
        first = generate_trial_environment(
            upgrade_contact_url="mailto:trial@example.com",
            trial_days=21,
            http_port=8765,
        )
        second = generate_trial_environment(
            upgrade_contact_url="https://example.com/upgrade"
        )

        for name in (
            "SRE_POSTGRES_PASSWORD",
            "SRE_ADMIN_API_KEY",
            "SRE_TRIAL_ACTIVATION_TOKEN",
            "EXECUTION_GUARD_TOKEN",
        ):
            self.assertGreaterEqual(len(first[name]), 32)
            self.assertNotEqual(first[name], second[name])
            self.assertNotIn("=", first[name])
        self.assertEqual(first["SRE_TRIAL_DAYS"], "21")
        self.assertEqual(first["SRE_HTTP_PORT"], "8765")

    def test_upgrade_contact_requires_https_or_mailto(self):
        self.assertEqual(
            validate_upgrade_contact_url("mailto:sales@example.com"),
            "mailto:sales@example.com",
        )
        self.assertEqual(
            validate_upgrade_contact_url("https://example.com/contact"),
            "https://example.com/contact",
        )
        for invalid in (
            "http://example.com/contact",
            "https://user:password@example.com/contact",
            "javascript:alert(1)",
            "",
        ):
            with self.assertRaises(ValueError):
                validate_upgrade_contact_url(invalid)
        with self.assertRaisesRegex(ValueError, "unsafe dotenv"):
            validate_upgrade_contact_url("https://example.com/upgrade?$HOME")

    def test_generation_rejects_invalid_trial_days_and_port(self):
        with self.assertRaises(ValueError):
            generate_trial_environment(
                upgrade_contact_url="mailto:sales@example.com",
                trial_days=0,
            )
        with self.assertRaises(ValueError):
            generate_trial_environment(
                upgrade_contact_url="mailto:sales@example.com",
                http_port=70000,
            )

    def test_env_file_does_not_overwrite_without_explicit_force(self):
        values = generate_trial_environment(
            upgrade_contact_url="mailto:sales@example.com"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / ".env.trial.local"
            written = write_trial_environment(output, values)
            content = written.read_text(encoding="utf-8")
            self.assertIn("SRE_ADMIN_API_KEY=", content)
            self.assertNotIn("replace_with", content)

            with self.assertRaises(FileExistsError):
                write_trial_environment(output, values)

            rotated = generate_trial_environment(
                upgrade_contact_url="https://example.com/upgrade"
            )
            write_trial_environment(output, rotated, force=True)
            updated = output.read_text(encoding="utf-8")
            self.assertIn(rotated["SRE_ADMIN_API_KEY"], updated)
            self.assertNotIn(values["SRE_ADMIN_API_KEY"], updated)

    def test_env_file_round_trips_a_human_workspace_name(self):
        values = generate_trial_environment(
            upgrade_contact_url="mailto:sales@example.com",
            workspace_id="customer-a",
            workspace_name="客户 A #1",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / ".env.trial.local"
            write_trial_environment(output, values)

            self.assertEqual(read_trial_environment(output), values)
            self.assertIn(
                'SRE_WORKSPACE_NAME="客户 A #1"',
                output.read_text(encoding="utf-8"),
            )

    def test_env_reader_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / ".env.trial.local"
            output.write_text(
                "SRE_HTTP_PORT=8000\nSRE_HTTP_PORT=9000\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate trial environment key"):
                read_trial_environment(output)

            output.write_text("lowercase=value\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid trial environment line"):
                read_trial_environment(output)


if __name__ == "__main__":
    unittest.main()
