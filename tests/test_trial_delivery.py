import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.generate_trial_env import read_trial_environment
from scripts.prepare_trial_delivery import (
    SECRET_KEYS,
    prepare_trial_delivery,
    validate_workspace_id,
    validate_workspace_name,
    verify_trial_delivery,
)


class TrialDeliveryTests(unittest.TestCase):
    def test_prepares_an_isolated_delivery_without_leaking_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            delivery = Path(temp_dir) / "customer-a"
            manifest = prepare_trial_delivery(
                delivery,
                workspace_id="customer-a",
                workspace_name="客户 A",
                upgrade_contact_url="mailto:sales@example.com",
                trial_days=21,
                http_port=8765,
                now=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )

            report = verify_trial_delivery(delivery)
            values = read_trial_environment(delivery / ".env.trial.local")
            nonsecret_content = (delivery / "delivery.json").read_text(
                encoding="utf-8"
            ) + (delivery / "README.md").read_text(encoding="utf-8")

            self.assertTrue(report["valid"])
            self.assertFalse(report["secrets_printed"])
            self.assertEqual(report["secret_count"], 4)
            self.assertEqual(manifest["compose_project_name"], "sre-trial-customer-a")
            self.assertEqual(values["SRE_WORKSPACE_ID"], "customer-a")
            self.assertEqual(values["SRE_WORKSPACE_NAME"], "客户 A")
            self.assertEqual(values["SRE_TRIAL_DAYS"], "21")
            self.assertEqual(values["SRE_HTTP_PORT"], "8765")
            for key in SECRET_KEYS:
                self.assertNotIn(values[key], nonsecret_content)
                self.assertEqual(len(manifest["secret_fingerprints"][key]), 16)

    def test_refuses_to_overwrite_or_rotate_an_existing_delivery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            delivery = Path(temp_dir) / "customer-a"
            prepare_trial_delivery(
                delivery,
                workspace_id="customer-a",
                workspace_name="Customer A",
                upgrade_contact_url="https://example.com/upgrade",
            )
            original = read_trial_environment(delivery / ".env.trial.local")

            with self.assertRaises(FileExistsError):
                prepare_trial_delivery(
                    delivery,
                    workspace_id="customer-a",
                    workspace_name="Customer A",
                    upgrade_contact_url="https://example.com/upgrade",
                )

            self.assertEqual(
                read_trial_environment(delivery / ".env.trial.local"),
                original,
            )
            self.assertTrue(verify_trial_delivery(delivery)["valid"])

    def test_verifier_detects_secret_and_public_metadata_tampering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            delivery = Path(temp_dir) / "customer-a"
            prepare_trial_delivery(
                delivery,
                workspace_id="customer-a",
                workspace_name="Customer A",
                upgrade_contact_url="mailto:sales@example.com",
            )
            env_path = delivery / ".env.trial.local"
            values = read_trial_environment(env_path)
            env_content = env_path.read_text(encoding="utf-8")
            env_path.write_text(
                env_content.replace(
                    values["SRE_TRIAL_ACTIVATION_TOKEN"],
                    "sre_trial_" + "x" * 48,
                ),
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                verify_trial_delivery(delivery)

            env_path.write_text(env_content, encoding="utf-8", newline="\n")
            manifest_path = delivery / "delivery.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["workspace_id"] = "another-customer"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaisesRegex(ValueError, "does not match manifest"):
                verify_trial_delivery(delivery)

    def test_rejects_unsafe_customer_identifiers_and_names(self):
        for invalid in ("Customer A", "-customer", "customer-", "customer_a", "a" * 41):
            with self.assertRaises(ValueError):
                validate_workspace_id(invalid)
        for invalid in ("", "Customer\nA", "Customer $A", "x" * 101):
            with self.assertRaises(ValueError):
                validate_workspace_name(invalid)

    def test_rejects_incomplete_delivery_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "incomplete"):
                verify_trial_delivery(Path(temp_dir))

    def test_verifier_rejects_invalid_secret_ownership_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            delivery = Path(temp_dir) / "customer-a"
            prepare_trial_delivery(
                delivery,
                workspace_id="customer-a",
                workspace_name="Customer A",
                upgrade_contact_url="mailto:sales@example.com",
            )
            manifest_path = delivery / "delivery.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["customer_secret_key"] = "SRE_ADMIN_API_KEY"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
                newline="\n",
            )

            with self.assertRaisesRegex(ValueError, "customer secret ownership"):
                verify_trial_delivery(delivery)

    def test_verifier_rejects_unexpected_environment_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            delivery = Path(temp_dir) / "customer-a"
            prepare_trial_delivery(
                delivery,
                workspace_id="customer-a",
                workspace_name="Customer A",
                upgrade_contact_url="mailto:sales@example.com",
            )
            env_path = delivery / ".env.trial.local"
            env_path.write_text(
                env_path.read_text(encoding="utf-8") + "UNEXPECTED=value\n",
                encoding="utf-8",
                newline="\n",
            )

            with self.assertRaisesRegex(ValueError, "unexpected keys"):
                verify_trial_delivery(delivery)

    def test_cli_reports_only_nonsecret_delivery_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            delivery = Path(temp_dir) / "customer-cli"
            result = subprocess.run(
                [
                    sys.executable,
                    str(
                        Path(__file__).parents[1]
                        / "scripts"
                        / "prepare_trial_delivery.py"
                    ),
                    "create",
                    "--workspace-id",
                    "customer-cli",
                    "--workspace-name",
                    "Customer CLI",
                    "--upgrade-contact-url",
                    "mailto:sales@example.com",
                    "--output-dir",
                    str(delivery),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            values = read_trial_environment(delivery / ".env.trial.local")

            self.assertIn('"secrets_printed": false', result.stdout)
            for key in SECRET_KEYS:
                self.assertNotIn(values[key], result.stdout)
                self.assertNotIn(values[key], result.stderr)


if __name__ == "__main__":
    unittest.main()
