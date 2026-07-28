import unittest
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request
from wtforms.validators import ValidationError

from forms.storage import MediaProviderConnectionForm, StorageCreateForm


def request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "scheme": "http",
        }
    )


class MediaProviderFormTests(unittest.TestCase):
    def test_create_form_requires_initial_cloudinary_credentials(self):
        form = StorageCreateForm(
            request(),
            data={
                "type": "media",
                "name": "media",
                "cloud_name": "valid-cloud",
                "cloudinary_region": "us",
                "cloudinary_api_key": "",
                "cloudinary_api_secret": "",
            },
            db=None,
            team=SimpleNamespace(id="a" * 32),
        )

        with self.assertRaisesRegex(ValidationError, "API key is required"):
            form.validate_cloudinary_api_key(form.cloudinary_api_key)
        with self.assertRaisesRegex(ValidationError, "API secret is required"):
            form.validate_cloudinary_api_secret(form.cloudinary_api_secret)

    def test_rotation_form_keeps_existing_credentials_when_fields_are_blank(self):
        storage = SimpleNamespace(
            credentials={
                "api_key": "existing-key",
                "api_secret": "existing-secret",
            }
        )
        form = MediaProviderConnectionForm(
            request(),
            data={
                "cloud_name": "valid-cloud",
                "cloudinary_region": "us",
                "media_folder": "apps/production",
                "cloudinary_api_key": "",
                "cloudinary_api_secret": "",
            },
            storage=storage,
        )

        with patch("forms.storage.get_settings") as get_settings:
            get_settings.return_value.env = "production"
            _, credentials = form.values()

        self.assertEqual("existing-key", credentials.api_key)
        self.assertEqual("existing-secret", credentials.api_secret)


if __name__ == "__main__":
    unittest.main()