import os
import string
import unittest
from unittest.mock import patch

from charmm_gui_cli.locales import i18n


class LanguageTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language()

    def test_environment_priority_and_neutral_fallthrough(self):
        cases = [
            ({"LC_ALL": "C.UTF-8", "LANG": "zh_CN.UTF-8"}, "zh"),
            ({"LANGUAGE": "zh_CN:en"}, "zh"),
            ({"LC_ALL": "POSIX"}, "en"),
            ({"CHARMM_GUI_CLI_LANG": "en", "LANG": "zh_CN.UTF-8"}, "en"),
            ({"LANG": "ja_JP.UTF-8"}, "en"),
            ({"LANGUAGE": "C:POSIX:zh-HK.UTF-8@variant"}, "zh"),
            ({"LC_ALL": "", "LC_MESSAGES": "ZH_tw@variant", "LANG": "en_US"}, "zh"),
            ({"LANGUAGE": "en:zh_CN", "LC_ALL": "zh_CN"}, "en"),
            ({}, "en"),
        ]
        for env, expected in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.assertEqual(i18n.detect_lang(), expected)

    def test_explicit_language_overrides_environment(self):
        with patch.dict(os.environ, {"CHARMM_GUI_CLI_LANG": "en"}, clear=True):
            self.assertEqual(i18n.detect_lang("zh"), "zh")

    def test_language_is_cached_until_startup_reset(self):
        with patch.dict(os.environ, {"LANG": "zh_CN"}, clear=True):
            i18n.set_language()
            os.environ["LANG"] = "en_US"
            self.assertEqual(i18n.get_language(), "zh")
            self.assertEqual(i18n.set_language(), "en")

    def test_catalog_versions_and_placeholders_match(self):
        fields = lambda text: {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}
        for key, versions in i18n.MESSAGES.items():
            with self.subTest(key=key):
                self.assertTrue(versions.get("zh"))
                self.assertTrue(versions.get("en"))
                self.assertEqual(fields(versions["zh"]), fields(versions["en"]))

    def test_fallback_and_formatting(self):
        i18n.set_language("zh")
        with patch.dict(i18n.MESSAGES, {"test": {"en": "Hello {name}"}}):
            self.assertEqual(i18n.t("test", name="user"), "Hello user")
        self.assertEqual(i18n.t("missing.key"), "missing.key")
        self.assertEqual(i18n.t("cli.error", message="test"), "错误：test")
