"""DEP09 optional equipment-only private gateway; fake clients, no network."""
import inspect
import secrets
from urllib.parse import quote
import unittest
from unittest.mock import patch
import config
from src.infra.jjc_equipment_read_runtime import EquipmentReadRuntime, EquipmentReadRuntimeError

USER=secrets.token_hex(8)
PASSWORD=secrets.token_hex(12)
URI=f"mongodb://{USER}:{PASSWORD}@original.invalid:27019/synthetic_db?authSource=admin&retryReads=false"


def runtime(override="",enabled=True,uri=URI):
    calls=[]
    class Client:
        def get_default_database(self):return object()
        def close(self):pass
    def factory(uri,**kwargs):calls.append((uri,kwargs));return Client()
    options={"enabled":enabled,"uri":uri,"client_factory":factory}
    if "host_override" in inspect.signature(EquipmentReadRuntime).parameters:options["host_override"]=override
    return EquipmentReadRuntime(**options),calls


class EquipmentPrivateHostOverrideTests(unittest.TestCase):
    def test_default_uri_is_unchanged_and_override_is_a_supported_parameter(self):
        reader,calls=runtime()
        reader.get_db();self.assertEqual(URI,calls[0][0])
        self.assertIn("host_override",inspect.signature(EquipmentReadRuntime).parameters)

    def test_rfc1918_only_replaces_host_preserving_every_other_uri_component(self):
        for host in ("10.0.0.1","172.16.0.1","172.31.255.254","192.168.1.2"):
            with patch.object(config,"MONGO_URI",URI):
                reader,calls=runtime(host)
                reader.get_db()
                self.assertEqual(URI,config.MONGO_URI,"private gateway is equipment-only, never a global client URI rewrite")
            self.assertEqual(URI.replace("original.invalid",host),calls[0][0])
            self.assertEqual(2000,calls[0][1]["socketTimeoutMS"])
            self.assertEqual(1500,calls[0][1]["serverSelectionTimeoutMS"])
            self.assertEqual(4,calls[0][1]["maxPoolSize"])

    def test_rejected_overrides_never_initialize_or_leak_original_uri(self):
        for host in ("8.8.8.8","127.0.0.1","localhost","example.invalid","10.0.0.1:27017",
                     "user@10.0.0.1","10.0.0.1/path","10.0.0.1?x=1","10.0.0.1\n",
                     "10.0.0.1#x","::1","169.254.1.1","0.0.0.0","224.0.0.1","192.0.2.1","198.18.0.1","100.64.0.1"," 10.0.0.1"):
            reader,calls=runtime(host)
            with self.subTest(host=host),self.assertRaises(EquipmentReadRuntimeError) as caught:
                reader.get_db()
            self.assertEqual([],calls)
            self.assertNotIn(PASSWORD,str(caught.exception))
            self.assertNotIn(URI,str(caught.exception))

    def test_encoded_authentication_and_query_are_preserved_byte_for_byte(self):
        uri=f"mongodb://{quote(USER+'@',safe='')}:{quote(PASSWORD+':',safe='')}@original.invalid:27019/synthetic_db?authSource=admin&tls=false"
        reader,calls=runtime("10.2.3.4",uri=uri)
        reader.get_db()
        self.assertEqual(uri.replace("original.invalid","10.2.3.4"),calls[0][0])

    def test_tls_or_ssl_true_override_is_rejected_without_relaxing_validation(self):
        for option in ("tls=true","ssl=true","TLS=True"):
            uri=URI+"&"+option;reader,calls=runtime("10.2.3.4",uri=uri)
            with self.assertRaises(EquipmentReadRuntimeError) as caught:reader.get_db()
            self.assertEqual([],calls)
            self.assertNotIn(PASSWORD,str(caught.exception))

    def test_disabled_runtime_does_not_validate_override_or_initialize(self):
        class Unreadable:
            def __str__(self):raise AssertionError("disabled feature must not validate override")
        reader,calls=runtime(Unreadable(),enabled=False)
        with self.assertRaisesRegex(EquipmentReadRuntimeError,"equipment_read_disabled"):reader.get_db()
        self.assertEqual([],calls)


if __name__=="__main__":unittest.main()
