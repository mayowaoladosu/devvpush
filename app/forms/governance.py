"""Team access, notification, and webhook forms."""

from email_validator import EmailNotValidError, validate_email
from starlette_wtf import StarletteForm
from wtforms import BooleanField, HiddenField, IntegerField, StringField, TextAreaField
from wtforms.validators import (
    URL,
    DataRequired,
    Length,
    NumberRange,
    Optional,
    ValidationError,
)

from dependencies import get_lazy_translation as _l

_SCOPE_FIELDS = {
    "scope_projects_read": "projects:read",
    "scope_projects_write": "projects:write",
    "scope_deployments_read": "deployments:read",
    "scope_deployments_write": "deployments:write",
    "scope_logs_read": "logs:read",
    "scope_audit_read": "audit:read",
    "scope_webhooks_read": "webhooks:read",
    "scope_webhooks_write": "webhooks:write",
}


class ApiTokenCreateForm(StarletteForm):
    name = StringField(
        _l("Token name"),
        validators=[DataRequired(), Length(min=1, max=100)],
    )
    expires_in_days = IntegerField(
        _l("Expires in days"),
        validators=[DataRequired(), NumberRange(min=1, max=3650)],
        default=90,
    )
    scope_projects_read = BooleanField(_l("Read projects"), default=True)
    scope_projects_write = BooleanField(_l("Write projects"), default=False)
    scope_deployments_read = BooleanField(_l("Read deployments"), default=True)
    scope_deployments_write = BooleanField(_l("Create and manage deployments"), default=True)
    scope_logs_read = BooleanField(_l("Read logs"), default=True)
    scope_audit_read = BooleanField(_l("Read audit history"), default=False)
    scope_webhooks_read = BooleanField(_l("Read webhooks"), default=False)
    scope_webhooks_write = BooleanField(_l("Manage webhooks"), default=False)

    def scopes(self) -> list[str]:
        selected = [
            scope
            for field_name, scope in _SCOPE_FIELDS.items()
            if bool(getattr(self, field_name).data)
        ]
        return selected


class ApiTokenRevokeForm(StarletteForm):
    token_id = HiddenField(validators=[DataRequired()])


class WebhookEndpointForm(StarletteForm):
    name = StringField(
        _l("Webhook name"),
        validators=[DataRequired(), Length(min=1, max=100)],
    )
    url = StringField(
        _l("Endpoint URL"),
        validators=[DataRequired(), Length(max=2048), URL(require_tld=False)],
    )
    deployment_created = BooleanField(_l("Deployment created"), default=True)
    deployment_succeeded = BooleanField(_l("Deployment succeeded"), default=True)
    deployment_failed = BooleanField(_l("Deployment failed"), default=True)
    deployment_canceled = BooleanField(_l("Deployment canceled"), default=True)
    deployment_skipped = BooleanField(_l("Deployment skipped"), default=True)

    def events(self) -> list[str]:
        mapping = {
            "deployment_created": "deployment.created",
            "deployment_succeeded": "deployment.succeeded",
            "deployment_failed": "deployment.failed",
            "deployment_canceled": "deployment.canceled",
            "deployment_skipped": "deployment.skipped",
        }
        return [
            event
            for field_name, event in mapping.items()
            if bool(getattr(self, field_name).data)
        ]


class WebhookEndpointDeleteForm(StarletteForm):
    endpoint_id = HiddenField(validators=[DataRequired()])


class NotificationSettingsForm(StarletteForm):
    deployment_succeeded = BooleanField(_l("Email successful deployments"))
    deployment_failed = BooleanField(_l("Email failed deployments"), default=True)
    deployment_canceled = BooleanField(_l("Email canceled deployments"))
    recipients = TextAreaField(
        _l("Recipients"),
        validators=[Optional(), Length(max=4000)],
    )

    def recipient_list(self) -> list[str]:
        raw = str(self.recipients.data or "")
        return list(
            dict.fromkeys(
                value.strip().lower()
                for value in raw.replace(",", "\n").splitlines()
                if value.strip()
            )
        )[:50]

    def validate_recipients(self, field) -> None:
        for value in self.recipient_list():
            try:
                validate_email(value, check_deliverability=False)
            except EmailNotValidError as exc:
                raise ValidationError(
                    f"Invalid notification recipient: {value}"
                ) from exc
