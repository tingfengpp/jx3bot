"""Isolated config execution; no real environment/runtime file is read."""
import io
import json
import logging
import os
from pathlib import Path
import unittest
from unittest.mock import patch

CONFIG = Path(__file__).resolve().parents[1] / "config.py"
KEYS = ("JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED",)


class EquipmentFlagConfig(unittest.TestCase):
    def load(self, *, env=None, runtime=None):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger()
        logger.addHandler(handler)
        try:
            with patch.dict(os.environ, env or {}, clear=True), patch("os.path.exists", return_value=runtime is not None), patch("builtins.open", return_value=io.StringIO(json.dumps(runtime or {}))):
                namespace = {"__name__": "jjc_equipment_isolated_config"}
                exec(compile(CONFIG.read_text(), str(CONFIG), "exec"), namespace)
            return namespace, output.getvalue()
        finally:
            logger.removeHandler(handler)

    def test_equipment_gateway_has_its_own_default_and_environment_field(self):
        cfg,unused=self.load()
        self.assertEqual("",cfg.get("JJC_EQUIPMENT_MONGO_HOST"))
        cfg,unused=self.load(env={"JJC_EQUIPMENT_MONGO_HOST":"10.2.3.4",
            "MONGO_URI":"mongodb://synthetic.invalid:27019/synthetic_db"})
        self.assertEqual("10.2.3.4",cfg.get("JJC_EQUIPMENT_MONGO_HOST"))
        self.assertEqual("mongodb://synthetic.invalid:27019/synthetic_db",cfg["MONGO_URI"])

    def test_invalid_environment_is_disabled_with_key_source_only_warning(self):
        secret = "private-equipment-env-value-DO-NOT-LOG"
        for key in KEYS:
            with self.subTest(key=key):
                config, logs = self.load(env={key: secret})
                self.assertEqual(0, config[key])
                self.assertIn(key, logs)
                self.assertIn("environment", logs)
                self.assertNotIn(secret, logs)

    def test_invalid_runtime_values_disable_and_warn_without_value(self):
        secret = "private-equipment-runtime-value-DO-NOT-LOG"
        for key in KEYS:
            for value in (secret, True, False, 1.0, None, "", 2):
                with self.subTest(key=key, value_type=type(value).__name__):
                    config, logs = self.load(env={key: "1"}, runtime={key: value})
                    self.assertEqual(0, config[key])
                    self.assertIn(key, logs)
                    self.assertIn("runtime_config.json", logs)
                    self.assertNotIn(secret, logs)

    def test_valid_values_and_absent_defaults_do_not_warn(self):
        for key in KEYS:
            for value in (0, 1, "0", "1", " 1 "):
                config, logs = self.load(runtime={key: value})
                self.assertEqual(int(str(value).strip()), config[key])
                self.assertEqual("", logs)
            config, logs = self.load()
            self.assertEqual(0, config[key])
            self.assertEqual("", logs)
