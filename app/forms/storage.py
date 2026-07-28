import json
from starlette.requests import Request
from starlette_wtf import StarletteForm
from wtforms import (
    HiddenField,
    StringField,
    SubmitField,
    SelectField,
    TextAreaField,
    BooleanField,
    PasswordField,
)
from wtforms.validators import DataRequired, Length, Regexp, ValidationError, Optional
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies import get_translation as _, get_lazy_translation as _l
from config import get_settings
from models import Project, Storage, StorageProject, Team
from services.object_storage import (
    ObjectStorageConfigurationError,
    ObjectStorageService,
)
from services.media_provider import (
    MediaProviderConfigurationError,
    MediaProviderService,
)
from services.storage import StorageConfigurationError, StorageService


def _parse_environment_ids(value):
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        parsed = value
    else:
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
    if parsed is None:
        return []
    if not isinstance(parsed, list):
        return None
    environment_ids = []
    for item in parsed:
        if item in (None, ""):
            continue
        if not isinstance(item, str):
            return None
        environment_ids.append(item)
    return list(dict.fromkeys(environment_ids))


class StorageCreateForm(StarletteForm):
    type = SelectField(
        _l("Type"),
        choices=[
            ("database", _("Database")),
            ("volume", _("Volume")),
            ("object", _("Object storage")),
            ("media", _("Cloudinary media")),
        ],
    )
    name = StringField(
        _l("Name"),
        validators=[
            DataRequired(),
            Length(min=1, max=100),
            Regexp(
                r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$",
                message=_l(
                    "Storage names can only contain letters, numbers, hyphens, underscores and dots. They cannot start or end with a dot, underscore or hyphen."
                ),
            ),
        ],
    )
    submit = SubmitField(_l("Create storage"))
    environment_ids = StringField(_l("Environments"), validators=[Optional()])
    mount_path = StringField(
        _l("Mount path"), validators=[Optional(), Length(max=255)]
    )
    provider = SelectField(
        _l("Provider"),
        choices=[
            ("aws", _("AWS S3")),
            ("r2", _("Cloudflare R2")),
            ("custom", _("S3-compatible")),
        ],
        validators=[Optional()],
    )
    bucket = StringField(_l("Bucket"), validators=[Optional(), Length(max=63)])
    region = StringField(_l("Region"), validators=[Optional(), Length(max=63)])
    account_id = StringField(
        _l("Cloudflare account ID"), validators=[Optional(), Length(max=32)]
    )
    endpoint_url = StringField(
        _l("Endpoint URL"), validators=[Optional(), Length(max=2048)]
    )
    public_url = StringField(
        _l("Public URL"), validators=[Optional(), Length(max=2048)]
    )
    access_key_id = StringField(
        _l("Access key ID"), validators=[Optional(), Length(max=128)]
    )
    secret_access_key = PasswordField(
        _l("Secret access key"), validators=[Optional(), Length(max=256)]
    )
    session_token = PasswordField(
        _l("Session token"), validators=[Optional(), Length(max=4096)]
    )
    path_style = BooleanField(_l("Use path-style URLs"), default=False)
    cloud_name = StringField(
        _l("Cloud name"), validators=[Optional(), Length(max=128)]
    )
    cloudinary_region = SelectField(
        _l("Cloudinary data center"),
        choices=[
            ("us", _("US (default)")),
            ("eu", _("Europe")),
            ("ap", _("Asia Pacific")),
        ],
        validators=[Optional()],
    )
    media_folder = StringField(
        _l("Default folder"), validators=[Optional(), Length(max=255)]
    )
    cloudinary_api_key = StringField(
        _l("API key"), validators=[Optional(), Length(max=128)]
    )
    cloudinary_api_secret = PasswordField(
        _l("API secret"), validators=[Optional(), Length(max=256)]
    )

    def __init__(
        self,
        request: Request,
        *args,
        db: AsyncSession,
        team: Team,
        project: Project | None = None,
        **kwargs,
    ):
        super().__init__(request, *args, **kwargs)
        self.db = db
        self.team = team
        self.project = project

    async def async_validate_name(self, field):
        if self.db and self.team:
            result = await self.db.execute(
                select(Storage).where(
                    func.lower(Storage.name) == field.data.lower(),
                    Storage.team_id == self.team.id,
                )
            )
            if result.scalar_one_or_none():
                raise ValidationError(
                    _(
                        "A storage with this name already exists in this team or is reserved."
                    )
                )

    def validate_environment_ids(self, field):
        if not self.project:
            return
        environment_ids = _parse_environment_ids(field.data)
        if environment_ids is None:
            raise ValidationError(_("Invalid environment selection."))
        field.data = environment_ids
        for environment_id in environment_ids:
            if not self.project.get_environment_by_id(environment_id):
                raise ValidationError(_("Environment not found."))

    def validate_mount_path(self, field):
        if self.type.data in {"object", "media"}:
            field.data = None
            return
        try:
            field.data = StorageService.normalize_mount_path(field.data)
        except StorageConfigurationError as exc:
            raise ValidationError(_(str(exc))) from exc

    def validate_secret_access_key(self, field):
        if self.type.data != "object":
            return
        try:
            self.object_values()
        except ObjectStorageConfigurationError as exc:
            raise ValidationError(_(str(exc))) from exc

    def object_values(self):
        settings = get_settings()
        config = ObjectStorageService.build_config(
            provider=self.provider.data,
            bucket=self.bucket.data,
            region=self.region.data,
            account_id=self.account_id.data,
            endpoint_url=self.endpoint_url.data,
            public_url=self.public_url.data,
            path_style=bool(self.path_style.data),
            allow_insecure=(
                settings.env == "development"
                or settings.object_storage_allow_insecure_endpoints
            ),
        )
        credentials = ObjectStorageService.build_credentials(
            access_key_id=self.access_key_id.data,
            secret_access_key=self.secret_access_key.data,
            session_token=self.session_token.data,
        )
        return config, credentials

    def validate_cloudinary_api_secret(self, field):
        if self.type.data != "media":
            return
        if not str(field.data or "").strip():
            raise ValidationError(_("Cloudinary API secret is required."))
        try:
            self.media_values()
        except MediaProviderConfigurationError as exc:
            raise ValidationError(_(str(exc))) from exc

    def validate_cloudinary_api_key(self, field):
        if self.type.data == "media" and not str(field.data or "").strip():
            raise ValidationError(_("Cloudinary API key is required."))

    def media_values(self):
        settings = get_settings()
        custom_api_base_url = (
            settings.cloudinary_api_base_url
            if settings.env == "development"
            else None
        )
        config = MediaProviderService.build_config(
            cloud_name=self.cloud_name.data,
            region=self.cloudinary_region.data,
            folder=self.media_folder.data,
            api_base_url=custom_api_base_url,
            allow_custom_endpoint=bool(custom_api_base_url),
            allow_insecure=settings.env == "development",
        )
        credentials = MediaProviderService.build_credentials(
            api_key=self.cloudinary_api_key.data,
            api_secret=self.cloudinary_api_secret.data,
        )
        return config, credentials


class StorageDeleteForm(StarletteForm):
    name = HiddenField(_l("Storage name"), validators=[DataRequired()])
    confirm = StringField(_l("Confirmation"), validators=[DataRequired()])
    submit = SubmitField(_l("Delete"), name="delete_storage")

    def validate_confirm(self, field):
        if field.data != self.name.data:  # type: ignore
            raise ValidationError(_("Storage name confirmation did not match."))


class StorageResetForm(StarletteForm):
    name = HiddenField(_l("Storage name"), validators=[DataRequired()])
    confirm = StringField(_l("Confirmation"), validators=[DataRequired()])
    submit = SubmitField(_l("Reset"), name="reset_storage")

    def validate_confirm(self, field):
        if field.data != self.name.data:  # type: ignore
            raise ValidationError(_("Storage name confirmation did not match."))


class StorageProjectForm(StarletteForm):
    association_id = HiddenField()
    storage_id = HiddenField(_l("Storage"), validators=[DataRequired()])
    project_id = StringField(_l("Project"), validators=[DataRequired()])
    environment_ids = StringField(_l("Environments"), validators=[Optional()])
    mount_path = StringField(
        _l("Mount path"), validators=[Optional(), Length(max=255)]
    )

    def __init__(
        self,
        request: Request,
        *args,
        storage: Storage | None = None,
        storages: list[Storage] | None = None,
        projects: list[Project],
        associations: list["StorageProject"],
        **kwargs,
    ):
        super().__init__(request, *args, **kwargs)
        self.storage = storage
        self.storages = storages or []
        self.projects = projects
        self.associations = associations
        self._projects_by_id = {project.id: project for project in projects}
        self._storages_by_id = {storage.id: storage for storage in self.storages}
        self._associations_by_id = {
            str(association.id): association for association in associations
        }
        self._selected_project = None
        self._selected_storage = storage
        self.association = None
        if self.environment_ids.data in (None, ""):
            self.environment_ids.data = []

    def _parse_environment_ids(self, value):
        return _parse_environment_ids(value)

    def validate_association_id(self, field):
        if not field.data:
            return
        association = self._associations_by_id.get(field.data)
        if not association:
            raise ValidationError(_("Association not found."))
        if self.storage and association.storage_id != self.storage.id:
            raise ValidationError(_("Association not found."))
        self.association = association

    def validate_storage_id(self, field):
        if self.storage:
            if field.data != self.storage.id:
                raise ValidationError(_("Storage not found."))
        elif self._storages_by_id:
            storage = self._storages_by_id.get(field.data)
            if not storage:
                raise ValidationError(_("Storage not found."))
            self._selected_storage = storage
        else:
            raise ValidationError(_("Storage not found."))
        if self.association and field.data != self.association.storage_id:
            raise ValidationError(_("Storage cannot be changed."))

    def validate_project_id(self, field):
        project = self._projects_by_id.get(field.data)
        if not project:
            raise ValidationError(_("Project not found."))
        if self.association and field.data != self.association.project_id:
            raise ValidationError(_("Project cannot be changed."))
        self._selected_project = project

    def validate_environment_ids(self, field):
        if not self._selected_project and self.project_id.data:
            self._selected_project = self._projects_by_id.get(self.project_id.data)
        if not self._selected_project:
            return
        environment_ids = self._parse_environment_ids(field.data)
        if environment_ids is None:
            raise ValidationError(_("Invalid environment selection."))
        environment_ids = list(dict.fromkeys(environment_ids))
        field.data = environment_ids
        for environment_id in environment_ids:
            if not self._selected_project.get_environment_by_id(environment_id):
                raise ValidationError(_("Environment not found."))
        association_id = self.association_id.data
        for association in self.associations:
            if association.project_id != self.project_id.data:
                continue
            if association.storage_id != self.storage_id.data:
                continue
            if association_id and str(association.id) == association_id:
                continue
            raise ValidationError(
                _("This project is already connected to this storage.")
            )

    def validate_mount_path(self, field):
        if (
            self._selected_storage
            and self._selected_storage.type in {"object", "media"}
        ):
            field.data = None
            return
        try:
            field.data = StorageService.normalize_mount_path(field.data)
        except StorageConfigurationError as exc:
            raise ValidationError(_(str(exc))) from exc


class StorageProjectRemoveForm(StarletteForm):
    association_id = HiddenField(_l("Association ID"), validators=[DataRequired()])
    confirm = StringField(_l("Confirmation"), validators=[DataRequired()])

    def __init__(
        self, request: Request, *args, associations: list["StorageProject"], **kwargs
    ):
        super().__init__(request, *args, **kwargs)
        self.associations = associations
        self._associations_by_id = {
            str(association.id): association for association in associations
        }
        self.association = None

    def validate_association_id(self, field):
        association = self._associations_by_id.get(field.data)
        if not association:
            raise ValidationError(_("Association not found."))
        self.association = association

    def validate_confirm(self, field):
        if not self.association:
            return
        project_name = self.association.project.name if self.association.project else ""
        if field.data != project_name:
            raise ValidationError(_("Project name confirmation did not match."))


class ObjectStorageConnectionForm(StarletteForm):
    provider = SelectField(
        _l("Provider"),
        choices=[
            ("aws", _("AWS S3")),
            ("r2", _("Cloudflare R2")),
            ("custom", _("S3-compatible")),
        ],
    )
    bucket = StringField(
        _l("Bucket"), validators=[DataRequired(), Length(max=63)]
    )
    region = StringField(_l("Region"), validators=[Optional(), Length(max=63)])
    account_id = StringField(
        _l("Cloudflare account ID"), validators=[Optional(), Length(max=32)]
    )
    endpoint_url = StringField(
        _l("Endpoint URL"), validators=[Optional(), Length(max=2048)]
    )
    public_url = StringField(
        _l("Public URL"), validators=[Optional(), Length(max=2048)]
    )
    path_style = BooleanField(_l("Use path-style URLs"), default=False)
    access_key_id = StringField(
        _l("Access key ID"), validators=[Optional(), Length(max=128)]
    )
    secret_access_key = PasswordField(
        _l("Secret access key"), validators=[Optional(), Length(max=256)]
    )
    session_token = PasswordField(
        _l("Session token"), validators=[Optional(), Length(max=4096)]
    )
    clear_session_token = BooleanField(_l("Remove the stored session token"))
    submit = SubmitField(_l("Verify and save"))

    def __init__(self, request: Request, *args, storage: Storage, **kwargs):
        super().__init__(request, *args, **kwargs)
        self.storage = storage

    def validate_secret_access_key(self, field):
        try:
            self.values()
        except ObjectStorageConfigurationError as exc:
            raise ValidationError(_(str(exc))) from exc

    def values(self):
        settings = get_settings()
        config = ObjectStorageService.build_config(
            provider=self.provider.data,
            bucket=self.bucket.data,
            region=self.region.data,
            account_id=self.account_id.data,
            endpoint_url=self.endpoint_url.data,
            public_url=self.public_url.data,
            path_style=bool(self.path_style.data),
            allow_insecure=(
                settings.env == "development"
                or settings.object_storage_allow_insecure_endpoints
            ),
        )
        existing = self.storage.credentials
        credentials = ObjectStorageService.build_credentials(
            access_key_id=self.access_key_id.data
            or existing.get("access_key_id"),
            secret_access_key=self.secret_access_key.data
            or existing.get("secret_access_key"),
            session_token=(
                None
                if self.clear_session_token.data
                else (
                    self.session_token.data
                    if self.session_token.data not in (None, "")
                    else existing.get("session_token")
                )
            ),
        )
        return config, credentials


class MediaProviderConnectionForm(StarletteForm):
    cloud_name = StringField(
        _l("Cloud name"), validators=[DataRequired(), Length(max=128)]
    )
    cloudinary_region = SelectField(
        _l("Cloudinary data center"),
        choices=[
            ("us", _("US (default)")),
            ("eu", _("Europe")),
            ("ap", _("Asia Pacific")),
        ],
    )
    media_folder = StringField(
        _l("Default folder"), validators=[Optional(), Length(max=255)]
    )
    cloudinary_api_key = StringField(
        _l("API key"), validators=[Optional(), Length(max=128)]
    )
    cloudinary_api_secret = PasswordField(
        _l("API secret"), validators=[Optional(), Length(max=256)]
    )
    submit = SubmitField(_l("Verify and save"))

    def __init__(self, request: Request, *args, storage: Storage, **kwargs):
        super().__init__(request, *args, **kwargs)
        self.storage = storage

    def validate_cloudinary_api_secret(self, field):
        try:
            self.values()
        except MediaProviderConfigurationError as exc:
            raise ValidationError(_(str(exc))) from exc

    def values(self):
        settings = get_settings()
        custom_api_base_url = (
            settings.cloudinary_api_base_url
            if settings.env == "development"
            else None
        )
        config = MediaProviderService.build_config(
            cloud_name=self.cloud_name.data,
            region=self.cloudinary_region.data,
            folder=self.media_folder.data,
            api_base_url=custom_api_base_url,
            allow_custom_endpoint=bool(custom_api_base_url),
            allow_insecure=settings.env == "development",
        )
        existing = self.storage.credentials
        credentials = MediaProviderService.build_credentials(
            api_key=self.cloudinary_api_key.data or existing.get("api_key"),
            api_secret=(
                self.cloudinary_api_secret.data or existing.get("api_secret")
            ),
        )
        return config, credentials


class StorageQueryForm(StarletteForm):
    query = TextAreaField(_l("SQL Query"), validators=[DataRequired()])
    write_mode = BooleanField(_l("Write mode"), default=False)
    submit = SubmitField(_l("Run"), name="run_query")
