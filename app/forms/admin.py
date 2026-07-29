from starlette_wtf import StarletteForm
from wtforms import (
    BooleanField,
    HiddenField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Optional, ValidationError

from dependencies import get_translation as _, get_lazy_translation as _l
from config import get_settings
from models import DeploymentNode
from services.deployment_nodes import (
    DeploymentNodeConfigurationError,
    DeploymentNodeService,
)


class AdminUserDeleteForm(StarletteForm):
    user_id = HiddenField(_l("User ID"), validators=[DataRequired()])
    email = HiddenField(_l("Email"), validators=[DataRequired()])
    confirm = StringField(_l("Confirmation"), validators=[DataRequired()])
    submit = SubmitField(_l("Delete"), name="admin_delete_user")

    def validate_confirm(self, field):
        if field.data != self.email.data:  # type: ignore
            raise ValidationError(_("Email confirmation did not match."))


class AllowlistAddForm(StarletteForm):
    type = SelectField(
        _l("Type"),
        choices=[
            ("email", _l("Email")),
            ("domain", _l("Domain")),
            ("pattern", _l("Pattern (regex)")),
        ],
        validators=[DataRequired()],
    )
    value = StringField(_l("Value"), validators=[DataRequired()])
    submit = SubmitField(_l("Add"), name="allowlist_add")


class AllowlistDeleteForm(StarletteForm):
    entry_id = HiddenField(_l("Entry ID"), validators=[DataRequired()])
    submit = SubmitField(_l("Delete"), name="allowlist_delete")


class AllowlistImportForm(StarletteForm):
    emails = TextAreaField(
        _l("Email addresses (one per line or comma-separated)"),
        validators=[DataRequired()],
    )
    submit = SubmitField(_l("Import"), name="allowlist_import")


class RegistryImageActionForm(StarletteForm):
    slug = HiddenField(_l("Slug"), validators=[Optional()])


class RegistryUpdateForm(StarletteForm):
    submit = SubmitField(_l("Update"))


class RunnerToggleForm(StarletteForm):
    slug = HiddenField(_l("Slug"), validators=[DataRequired()])
    enabled = BooleanField(_l("Enabled"))


class PresetToggleForm(StarletteForm):
    slug = HiddenField(_l("Slug"), validators=[DataRequired()])
    enabled = BooleanField(_l("Enabled"))


class DeploymentNodeForm(StarletteForm):
    node_id = HiddenField()
    name = StringField(_l("Name"), validators=[DataRequired()])
    endpoint_url = StringField(
        _l("Agent endpoint"), validators=[DataRequired()]
    )
    runtime_host = StringField(
        _l("Runtime host"), validators=[DataRequired()]
    )
    region = StringField(_l("Region"), validators=[DataRequired()])
    max_deployments = IntegerField(
        _l("Maximum deployments"), validators=[DataRequired()]
    )
    token = PasswordField(_l("Agent token"), validators=[Optional()])
    submit = SubmitField(_l("Verify and save"))

    def __init__(
        self,
        *args,
        node: DeploymentNode | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.node = node

    def validate_token(self, field):
        value = field.data or (self.node.token if self.node else "")
        try:
            DeploymentNodeService.build_token(value)
        except DeploymentNodeConfigurationError as exc:
            raise ValidationError(_(str(exc))) from exc

    def values(self):
        settings = get_settings()
        config = DeploymentNodeService.build_config(
            name=self.name.data,
            endpoint_url=self.endpoint_url.data,
            runtime_host=self.runtime_host.data,
            region=self.region.data,
            max_deployments=self.max_deployments.data,
            allow_insecure=(
                settings.env == "development"
                or settings.deployment_node_allow_insecure_endpoints
            ),
        )
        token = DeploymentNodeService.build_token(
            self.token.data or (self.node.token if self.node else "")
        )
        return config, token


class DeploymentNodeActionForm(StarletteForm):
    node_id = HiddenField(_l("Node ID"), validators=[DataRequired()])
    action = HiddenField(_l("Action"), validators=[DataRequired()])
    confirm = StringField(_l("Confirmation"), validators=[Optional()])
