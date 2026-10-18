"""Cognito user pools and their clients, domains, identity providers, resource
servers, groups, identity pools and config secrets, mirroring the Terraform
`AWS-Cognito` module.

    cognito:
      - name: portal
        username_attributes: [email]
        mfa_configuration: OPTIONAL
        software_token_mfa_enabled: true
        lambda_config:
          pre_sign_up: signup-check        # a function from `lambdas`, or an ARN
        domain:
          domain_name: auth.example.com
          certificate: auth                # from `certificates` (us-east-1)
        clients:
          - name: web
            allowed_oauth_flows: [code]
            allowed_oauth_flows_user_pool_client: true
            allowed_oauth_scopes: [openid, email]
            callback_urls: [https://app.example.com/callback]
            supported_identity_providers: [COGNITO, Okta]
        identity_providers:
          - provider_name: Okta
            provider_type: SAML
            provider_details:
              MetadataURL: https://example.okta.com/app/metadata
        secret:
          create: true                     # IDs only; client secrets are never copied

User pools are retained on stack deletion and default to deletion protection
`ACTIVE`; set `removal_policy: destroy` to allow both to be removed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from cognitech_cdk.common import schema
from cognitech_cdk.constructs.lambdas import LAMBDA_ARN, check_unique_ids

IAM_ROLE_ARN = re.compile(r"arn:aws[a-z-]*:iam::[0-9]{12}:role/[\w+=,.@/-]+")
ANY_ARN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{0,12}:.+")
POOL_NAME = re.compile(r"[\w\s+=,.@-]{1,128}")
DOMAIN_PREFIX = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
CUSTOM_DOMAIN = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
)
SECRET_REFERENCE = re.compile(r"\{\{resolve:secretsmanager:[^{}]+\}\}")
SECRET_DETAILS = {"client_secret", "private_key"}
SENSITIVE_KEY = re.compile(r"secret|password|passwd|token|private|credential", re.IGNORECASE)

LAMBDA_TRIGGERS = {
    "create_auth_challenge": "CreateAuthChallenge",
    "custom_message": "CustomMessage",
    "define_auth_challenge": "DefineAuthChallenge",
    "post_authentication": "PostAuthentication",
    "post_confirmation": "PostConfirmation",
    "pre_authentication": "PreAuthentication",
    "pre_sign_up": "PreSignUp",
    "pre_token_generation": "PreTokenGeneration",
    "user_migration": "UserMigration",
    "verify_auth_challenge_response": "VerifyAuthChallengeResponse",
}
AUTH_FLOWS = {
    "ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_CUSTOM_AUTH", "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_AUTH",
}
OAUTH_FLOWS = {"code", "implicit", "client_credentials"}
TOKEN_UNITS = {"seconds", "minutes", "hours", "days"}
PROVIDER_TYPES = {"SAML", "Google", "Facebook", "LoginWithAmazon", "SignInWithApple", "OIDC"}
ATTRIBUTE_TYPES = {"String", "Number", "DateTime", "Boolean"}


def _choice(value: Any, label: str, choices: set[str] | tuple[str, ...], default: str) -> str:
    result = schema.string(value, label, default=default)
    if result not in choices:
        raise ValueError(f"{label} must be one of {', '.join(sorted(choices))}.")
    return result


def _subset(
    value: Any, label: str, choices: set[str], *, default: tuple[str, ...] | None = ()
) -> tuple[str, ...] | None:
    if value is None:
        return default
    result = schema.strings(value, label)
    unknown = sorted(set(result) - choices)
    if unknown:
        raise ValueError(
            f"{label}: unknown values {', '.join(unknown)}; expected {', '.join(sorted(choices))}."
        )
    if len(set(result)) != len(result):
        raise ValueError(f"{label}: duplicate value.")
    return result


def _optional_strings(value: Any, label: str) -> tuple[str, ...] | None:
    return None if value is None else schema.strings(value, label)


def _arn(value: Any, label: str, pattern: re.Pattern[str] = ANY_ARN) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} must be a valid ARN.")
    return value


def _urls(value: Any, label: str) -> tuple[str, ...]:
    urls = schema.strings(value, label)
    for url in urls:
        if not (
            url.startswith("https://")
            or re.match(r"http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)", url)
        ):
            raise ValueError(f"{label}: '{url}' must use https (http only for localhost).")
    return urls


@dataclass(frozen=True)
class PasswordPolicyProps:
    minimum_length: int = 8
    require_lowercase: bool = True
    require_numbers: bool = True
    require_symbols: bool = True
    require_uppercase: bool = True
    temporary_password_validity_days: int = 7

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> PasswordPolicyProps:
        if raw is None:
            return cls()
        data = schema.mapping(raw, label, {
            "minimum_length", "require_lowercase", "require_numbers", "require_symbols",
            "require_uppercase", "temporary_password_validity_days",
        })
        return cls(
            minimum_length=schema.integer(
                data.get("minimum_length"), f"{label}.minimum_length",
                default=8, minimum=6, maximum=99,
            ),
            require_lowercase=schema.boolean(
                data.get("require_lowercase"), f"{label}.require_lowercase", default=True
            ),
            require_numbers=schema.boolean(
                data.get("require_numbers"), f"{label}.require_numbers", default=True
            ),
            require_symbols=schema.boolean(
                data.get("require_symbols"), f"{label}.require_symbols", default=True
            ),
            require_uppercase=schema.boolean(
                data.get("require_uppercase"), f"{label}.require_uppercase", default=True
            ),
            temporary_password_validity_days=schema.integer(
                data.get("temporary_password_validity_days"),
                f"{label}.temporary_password_validity_days", default=7, minimum=0, maximum=365,
            ),
        )


@dataclass(frozen=True)
class SmsConfigurationProps:
    external_id: str
    sns_caller_arn: str
    sns_region: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> SmsConfigurationProps | None:
        if raw is None:
            return None
        data = schema.mapping(
            raw, label, {"external_id", "sns_caller_arn", "sns_region"},
            required=("external_id", "sns_caller_arn"),
        )
        return cls(
            external_id=schema.required_string(data["external_id"], f"{label}.external_id"),
            sns_caller_arn=_arn(data["sns_caller_arn"], f"{label}.sns_caller_arn", IAM_ROLE_ARN),
            sns_region=schema.string(data.get("sns_region"), f"{label}.sns_region"),
        )


@dataclass(frozen=True)
class EmailConfigurationProps:
    email_sending_account: str = "COGNITO_DEFAULT"
    from_email_address: str | None = None
    reply_to_email_address: str | None = None
    source_arn: str | None = None
    configuration_set: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> EmailConfigurationProps:
        if raw is None:
            return cls()
        data = schema.mapping(raw, label, {
            "email_sending_account", "from_email_address", "reply_to_email_address",
            "source_arn", "configuration_set",
        })
        result = cls(
            email_sending_account=_choice(
                data.get("email_sending_account"), f"{label}.email_sending_account",
                ("COGNITO_DEFAULT", "DEVELOPER"), "COGNITO_DEFAULT",
            ),
            from_email_address=schema.string(
                data.get("from_email_address"), f"{label}.from_email_address"
            ),
            reply_to_email_address=schema.string(
                data.get("reply_to_email_address"), f"{label}.reply_to_email_address"
            ),
            source_arn=_arn(data.get("source_arn"), f"{label}.source_arn"),
            configuration_set=schema.string(
                data.get("configuration_set"), f"{label}.configuration_set"
            ),
        )
        if result.email_sending_account == "DEVELOPER" and result.source_arn is None:
            raise ValueError(f"{label}: DEVELOPER email sending needs a source_arn (SES identity).")
        return result


@dataclass(frozen=True)
class AdminCreateUserProps:
    allow_admin_create_user_only: bool = False
    invite_email_subject: str | None = None
    invite_email_message: str | None = None
    invite_sms_message: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> AdminCreateUserProps:
        if raw is None:
            return cls()
        data = schema.mapping(raw, label, {
            "allow_admin_create_user_only", "invite_email_subject",
            "invite_email_message", "invite_sms_message",
        })
        return cls(
            allow_admin_create_user_only=schema.boolean(
                data.get("allow_admin_create_user_only"),
                f"{label}.allow_admin_create_user_only",
            ),
            invite_email_subject=schema.string(
                data.get("invite_email_subject"), f"{label}.invite_email_subject"
            ),
            invite_email_message=schema.string(
                data.get("invite_email_message"), f"{label}.invite_email_message"
            ),
            invite_sms_message=schema.string(
                data.get("invite_sms_message"), f"{label}.invite_sms_message"
            ),
        )

    @property
    def has_invite_template(self) -> bool:
        return any((self.invite_email_subject, self.invite_email_message, self.invite_sms_message))


@dataclass(frozen=True)
class SchemaAttributeProps:
    name: str
    attribute_data_type: str
    developer_only_attribute: bool = False
    mutable: bool = True
    required: bool = False
    string_constraints: Mapping[str, str] | None = None
    number_constraints: Mapping[str, str] | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> SchemaAttributeProps:
        data = schema.mapping(raw, f"{label} entry", {
            "name", "attribute_data_type", "developer_only_attribute", "mutable",
            "required", "string_constraints", "number_constraints",
        }, required=("name", "attribute_data_type"))
        name = schema.required_string(data["name"], f"{label} name")
        if not re.fullmatch(r"[\w:-]{1,20}", name):
            raise ValueError(f"{label} name '{name}' must be 1-20 word characters.")
        label = f"{label} '{name}'"

        def constraints(key: str, allowed: set[str]) -> Mapping[str, str] | None:
            raw_constraints = data.get(key)
            if raw_constraints is None:
                return None
            values = schema.string_map(
                schema.mapping(raw_constraints, f"{label}.{key}", allowed), f"{label}.{key}"
            )
            for item_key, item in values.items():
                if not re.fullmatch(r"-?\d+", item):
                    raise ValueError(f"{label}.{key}.{item_key} must be a whole number.")
            return values

        return cls(
            name=name,
            attribute_data_type=_choice(
                data["attribute_data_type"], f"{label}.attribute_data_type",
                ATTRIBUTE_TYPES, "String",
            ),
            developer_only_attribute=schema.boolean(
                data.get("developer_only_attribute"), f"{label}.developer_only_attribute"
            ),
            mutable=schema.boolean(data.get("mutable"), f"{label}.mutable", default=True),
            required=schema.boolean(data.get("required"), f"{label}.required"),
            string_constraints=constraints("string_constraints", {"min_length", "max_length"}),
            number_constraints=constraints("number_constraints", {"min_value", "max_value"}),
        )


@dataclass(frozen=True)
class VerificationMessageProps:
    default_email_option: str = "CONFIRM_WITH_CODE"
    email_message: str | None = None
    email_message_by_link: str | None = None
    email_subject: str | None = None
    email_subject_by_link: str | None = None
    sms_message: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> VerificationMessageProps:
        if raw is None:
            return cls()
        keys = ("email_message", "email_message_by_link", "email_subject",
                "email_subject_by_link", "sms_message")
        data = schema.mapping(raw, label, {"default_email_option", *keys})
        return cls(
            default_email_option=_choice(
                data.get("default_email_option"), f"{label}.default_email_option",
                ("CONFIRM_WITH_CODE", "CONFIRM_WITH_LINK"), "CONFIRM_WITH_CODE",
            ),
            **{key: schema.string(data.get(key), f"{label}.{key}") for key in keys},
        )


@dataclass(frozen=True)
class DomainProps:
    domain_name: str
    certificate: str | None = None
    certificate_arn: str | None = None

    @property
    def custom(self) -> bool:
        return self.certificate is not None or self.certificate_arn is not None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> DomainProps | None:
        if raw is None:
            return None
        data = schema.mapping(
            raw, label, {"domain_name", "certificate", "certificate_arn"},
            required=("domain_name",),
        )
        domain = schema.required_string(data["domain_name"], f"{label}.domain_name")
        certificate = data.get("certificate")
        certificate_arn = data.get("certificate_arn")
        if certificate is not None and certificate_arn is not None:
            raise ValueError(f"{label}: provide only one of certificate or certificate_arn.")
        if certificate is not None:
            schema.name(certificate, f"{label}.certificate")
        _arn(certificate_arn, f"{label}.certificate_arn")
        result = cls(domain, certificate, certificate_arn)
        if result.custom:
            if not CUSTOM_DOMAIN.fullmatch(domain):
                raise ValueError(
                    f"{label}.domain_name must be a lowercase DNS name for a custom domain."
                )
        elif not DOMAIN_PREFIX.fullmatch(domain) or any(
            word in domain for word in ("aws", "amazon", "cognito")
        ):
            raise ValueError(
                f"{label}.domain_name must be a lowercase prefix (letters, digits, '-') "
                "without 'aws', 'amazon' or 'cognito'; custom domains need a certificate."
            )
        return result


@dataclass(frozen=True)
class ClientProps:
    name: str
    generate_secret: bool = False
    explicit_auth_flows: tuple[str, ...] = ("ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH")
    allowed_oauth_flows: tuple[str, ...] = ()
    allowed_oauth_flows_user_pool_client: bool = False
    allowed_oauth_scopes: tuple[str, ...] = ()
    callback_urls: tuple[str, ...] = ()
    logout_urls: tuple[str, ...] = ()
    supported_identity_providers: tuple[str, ...] = ("COGNITO",)
    prevent_user_existence_errors: str = "ENABLED"
    refresh_token_validity: int = 30
    access_token_validity: int = 60
    id_token_validity: int = 60
    access_token_unit: str = "minutes"
    id_token_unit: str = "minutes"
    refresh_token_unit: str = "days"
    read_attributes: tuple[str, ...] | None = None
    write_attributes: tuple[str, ...] | None = None
    enable_token_revocation: bool = True
    enable_propagate_additional_user_context_data: bool = False
    default_redirect_uri: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> ClientProps:
        data = schema.mapping(raw, f"{label} entry", {
            "name", "generate_secret", "explicit_auth_flows", "allowed_oauth_flows",
            "allowed_oauth_flows_user_pool_client", "allowed_oauth_scopes", "callback_urls",
            "logout_urls", "supported_identity_providers", "prevent_user_existence_errors",
            "refresh_token_validity", "access_token_validity", "id_token_validity",
            "token_validity_units", "read_attributes", "write_attributes",
            "enable_token_revocation", "enable_propagate_additional_user_context_data",
            "default_redirect_uri",
        }, required=("name",))
        name = schema.name(data["name"], f"{label} name")
        label = f"{label} '{name}'"
        units = schema.mapping(
            data.get("token_validity_units") or {}, f"{label}.token_validity_units",
            {"access_token", "id_token", "refresh_token"},
        )
        result = cls(
            name=name,
            generate_secret=schema.boolean(data.get("generate_secret"), f"{label}.generate_secret"),
            explicit_auth_flows=_subset(
                data.get("explicit_auth_flows"), f"{label}.explicit_auth_flows", AUTH_FLOWS,
                default=("ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"),
            ),
            allowed_oauth_flows=_subset(
                data.get("allowed_oauth_flows"), f"{label}.allowed_oauth_flows", OAUTH_FLOWS
            ),
            allowed_oauth_flows_user_pool_client=schema.boolean(
                data.get("allowed_oauth_flows_user_pool_client"),
                f"{label}.allowed_oauth_flows_user_pool_client",
            ),
            allowed_oauth_scopes=schema.strings(
                data.get("allowed_oauth_scopes"), f"{label}.allowed_oauth_scopes"
            ),
            callback_urls=_urls(data.get("callback_urls"), f"{label}.callback_urls"),
            logout_urls=_urls(data.get("logout_urls"), f"{label}.logout_urls"),
            supported_identity_providers=schema.strings(
                data.get("supported_identity_providers", ["COGNITO"]),
                f"{label}.supported_identity_providers",
            ),
            prevent_user_existence_errors=_choice(
                data.get("prevent_user_existence_errors"),
                f"{label}.prevent_user_existence_errors", ("ENABLED", "LEGACY"), "ENABLED",
            ),
            refresh_token_validity=schema.integer(
                data.get("refresh_token_validity"), f"{label}.refresh_token_validity",
                default=30, minimum=1,
            ),
            access_token_validity=schema.integer(
                data.get("access_token_validity"), f"{label}.access_token_validity",
                default=60, minimum=1,
            ),
            id_token_validity=schema.integer(
                data.get("id_token_validity"), f"{label}.id_token_validity",
                default=60, minimum=1,
            ),
            access_token_unit=_choice(
                units.get("access_token"), f"{label}.token_validity_units.access_token",
                TOKEN_UNITS, "minutes",
            ),
            id_token_unit=_choice(
                units.get("id_token"), f"{label}.token_validity_units.id_token",
                TOKEN_UNITS, "minutes",
            ),
            refresh_token_unit=_choice(
                units.get("refresh_token"), f"{label}.token_validity_units.refresh_token",
                TOKEN_UNITS, "days",
            ),
            read_attributes=_optional_strings(
                data.get("read_attributes"), f"{label}.read_attributes"
            ),
            write_attributes=_optional_strings(
                data.get("write_attributes"), f"{label}.write_attributes"
            ),
            enable_token_revocation=schema.boolean(
                data.get("enable_token_revocation"), f"{label}.enable_token_revocation",
                default=True,
            ),
            enable_propagate_additional_user_context_data=schema.boolean(
                data.get("enable_propagate_additional_user_context_data"),
                f"{label}.enable_propagate_additional_user_context_data",
            ),
            default_redirect_uri=schema.string(
                data.get("default_redirect_uri"), f"{label}.default_redirect_uri"
            ),
        )
        if result.allowed_oauth_flows and not result.allowed_oauth_flows_user_pool_client:
            raise ValueError(
                f"{label}: allowed_oauth_flows need allowed_oauth_flows_user_pool_client: true."
            )
        if result.allowed_oauth_flows_user_pool_client and not result.allowed_oauth_flows:
            raise ValueError(f"{label}: OAuth is enabled but allowed_oauth_flows is empty.")
        if "client_credentials" in result.allowed_oauth_flows and not result.generate_secret:
            raise ValueError(f"{label}: client_credentials needs generate_secret: true.")
        if (
            result.enable_propagate_additional_user_context_data
            and not result.generate_secret
        ):
            raise ValueError(
                f"{label}: enable_propagate_additional_user_context_data needs generate_secret."
            )
        if (
            result.default_redirect_uri is not None
            and result.default_redirect_uri not in result.callback_urls
        ):
            raise ValueError(f"{label}: default_redirect_uri must be one of callback_urls.")
        return result


@dataclass(frozen=True)
class ResourceServerProps:
    identifier: str
    name: str
    scopes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> ResourceServerProps:
        data = schema.mapping(
            raw, f"{label} entry", {"identifier", "name", "scopes"},
            required=("identifier", "name"),
        )
        identifier = schema.required_string(data["identifier"], f"{label} identifier")
        label = f"{label} '{identifier}'"
        scopes = []
        raw_scopes = data.get("scopes") or []
        if not isinstance(raw_scopes, list):
            raise ValueError(f"{label}.scopes must be a list.")
        for scope in raw_scopes:
            item = schema.mapping(
                scope, f"{label} scope", {"scope_name", "scope_description"},
                required=("scope_name", "scope_description"),
            )
            scopes.append((
                schema.required_string(item["scope_name"], f"{label} scope_name"),
                schema.required_string(item["scope_description"], f"{label} scope_description"),
            ))
        if len({name for name, _ in scopes}) != len(scopes):
            raise ValueError(f"{label}: duplicate scope_name.")
        return cls(
            identifier=identifier,
            name=schema.required_string(data["name"], f"{label}.name"),
            scopes=tuple(scopes),
        )


@dataclass(frozen=True)
class UserGroupProps:
    name: str
    description: str | None = None
    precedence: int | None = None
    role_arn: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> UserGroupProps:
        data = schema.mapping(
            raw, f"{label} entry", {"name", "description", "precedence", "role_arn"},
            required=("name",),
        )
        name = schema.required_string(data["name"], f"{label} name")
        label = f"{label} '{name}'"
        return cls(
            name=name,
            description=schema.string(data.get("description"), f"{label}.description"),
            precedence=schema.integer(data.get("precedence"), f"{label}.precedence", minimum=0),
            role_arn=_arn(data.get("role_arn"), f"{label}.role_arn", IAM_ROLE_ARN),
        )


@dataclass(frozen=True)
class IdentityProviderProps:
    provider_name: str
    provider_type: str
    provider_details: Mapping[str, str]
    attribute_mapping: Mapping[str, str] = field(default_factory=dict)
    idp_identifiers: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> IdentityProviderProps:
        data = schema.mapping(raw, f"{label} entry", {
            "provider_name", "provider_type", "provider_details", "attribute_mapping",
            "idp_identifiers",
        }, required=("provider_name", "provider_type", "provider_details"))
        name = schema.required_string(data["provider_name"], f"{label} provider_name")
        if name == "COGNITO" or not re.fullmatch(r"[^_\s]\S{0,31}", name):
            raise ValueError(f"{label} provider_name '{name}' is not a valid provider name.")
        label = f"{label} '{name}'"
        details = schema.string_map(data["provider_details"], f"{label}.provider_details")
        if not details:
            raise ValueError(f"{label}.provider_details must not be empty.")
        for key, value in details.items():
            if key.casefold() in SECRET_DETAILS and not SECRET_REFERENCE.fullmatch(value):
                raise ValueError(
                    f"{label}.provider_details.{key} must be a Secrets Manager dynamic "
                    "reference such as '{{resolve:secretsmanager:name:SecretString:key}}'; "
                    "secret values are never written into templates."
                )
        return cls(
            provider_name=name,
            provider_type=_choice(
                data["provider_type"], f"{label}.provider_type", PROVIDER_TYPES, "SAML"
            ),
            provider_details=details,
            attribute_mapping=schema.string_map(
                data.get("attribute_mapping"), f"{label}.attribute_mapping"
            ),
            idp_identifiers=schema.strings(data.get("idp_identifiers"), f"{label}.idp_identifiers"),
        )


@dataclass(frozen=True)
class AdditionalProviderProps:
    client_id: str
    provider_name: str
    server_side_token_check: bool = False


@dataclass(frozen=True)
class IdentityPoolProps:
    create: bool = False
    name: str | None = None
    allow_unauthenticated_identities: bool = False
    allow_classic_flow: bool = False
    authenticated_role_arn: str | None = None
    unauthenticated_role_arn: str | None = None
    server_side_token_check: bool = False
    additional_cognito_providers: tuple[AdditionalProviderProps, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> IdentityPoolProps:
        if raw is None:
            return cls()
        data = schema.mapping(raw, label, {
            "create", "name", "allow_unauthenticated_identities", "allow_classic_flow",
            "authenticated_role_arn", "unauthenticated_role_arn", "server_side_token_check",
            "additional_cognito_providers",
        })
        name = schema.string(data.get("name"), f"{label}.name")
        if name is not None and not POOL_NAME.fullmatch(name):
            raise ValueError(f"{label}.name must be 1-128 word characters.")
        providers = []
        raw_providers = data.get("additional_cognito_providers") or []
        if not isinstance(raw_providers, list):
            raise ValueError(f"{label}.additional_cognito_providers must be a list.")
        for provider in raw_providers:
            item = schema.mapping(
                provider, f"{label}.additional_cognito_providers entry",
                {"client_id", "provider_name", "server_side_token_check"},
                required=("client_id", "provider_name"),
            )
            providers.append(AdditionalProviderProps(
                client_id=schema.required_string(item["client_id"], f"{label} client_id"),
                provider_name=schema.required_string(
                    item["provider_name"], f"{label} provider_name"
                ),
                server_side_token_check=schema.boolean(
                    item.get("server_side_token_check"), f"{label} server_side_token_check"
                ),
            ))
        result = cls(
            create=schema.boolean(data.get("create"), f"{label}.create"),
            name=name,
            allow_unauthenticated_identities=schema.boolean(
                data.get("allow_unauthenticated_identities"),
                f"{label}.allow_unauthenticated_identities",
            ),
            allow_classic_flow=schema.boolean(
                data.get("allow_classic_flow"), f"{label}.allow_classic_flow"
            ),
            authenticated_role_arn=_arn(
                data.get("authenticated_role_arn"), f"{label}.authenticated_role_arn",
                IAM_ROLE_ARN,
            ),
            unauthenticated_role_arn=_arn(
                data.get("unauthenticated_role_arn"), f"{label}.unauthenticated_role_arn",
                IAM_ROLE_ARN,
            ),
            server_side_token_check=schema.boolean(
                data.get("server_side_token_check"), f"{label}.server_side_token_check"
            ),
            additional_cognito_providers=tuple(providers),
        )
        if result.unauthenticated_role_arn and not result.allow_unauthenticated_identities:
            raise ValueError(
                f"{label}: unauthenticated_role_arn needs allow_unauthenticated_identities."
            )
        return result


@dataclass(frozen=True)
class SecretProps:
    """A Secrets Manager copy of the pool's identifiers (never client secrets)."""

    create: bool = False
    name: str | None = None
    description: str | None = None
    kms_key_id: str | None = None
    primary_client_name: str | None = None
    additional_values: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> SecretProps:
        if raw is None:
            return cls()
        data = schema.mapping(raw, label, {
            "create", "name", "description", "kms_key_id", "primary_client_name",
            "additional_values",
        })
        name = schema.string(data.get("name"), f"{label}.name")
        if name is not None and not re.fullmatch(r"[\w/+=.@-]{1,512}", name):
            raise ValueError(f"{label}.name must use Secrets Manager name characters.")
        primary = data.get("primary_client_name")
        if primary is not None:
            schema.name(primary, f"{label}.primary_client_name")
        additional = schema.string_map(
            data.get("additional_values"), f"{label}.additional_values"
        )
        sensitive = sorted(key for key in additional if SENSITIVE_KEY.search(key))
        if sensitive:
            raise ValueError(
                f"{label}.additional_values: {', '.join(sensitive)} look like secrets; "
                "values here are committed to the template, so store secrets separately."
            )
        return cls(
            create=schema.boolean(data.get("create"), f"{label}.create"),
            name=name,
            description=schema.string(data.get("description"), f"{label}.description"),
            kms_key_id=schema.string(data.get("kms_key_id"), f"{label}.kms_key_id"),
            primary_client_name=primary,
            additional_values=additional,
        )


@dataclass(frozen=True)
class UserPoolProps:
    name: str
    removal_policy: str = "retain"
    deletion_protection: str = "ACTIVE"
    alias_attributes: tuple[str, ...] | None = None
    username_attributes: tuple[str, ...] | None = None
    auto_verified_attributes: tuple[str, ...] = ("email",)
    mfa_configuration: str = "OFF"
    software_token_mfa_enabled: bool = False
    username_case_sensitive: bool = False
    password_policy: PasswordPolicyProps = PasswordPolicyProps()
    sms_configuration: SmsConfigurationProps | None = None
    email_configuration: EmailConfigurationProps = EmailConfigurationProps()
    admin_create_user_config: AdminCreateUserProps = AdminCreateUserProps()
    challenge_required_on_new_device: bool = False
    device_only_remembered_on_user_prompt: bool = False
    advanced_security_mode: str = "OFF"
    schema_attributes: tuple[SchemaAttributeProps, ...] = ()
    verification_message_template: VerificationMessageProps = VerificationMessageProps()
    lambda_triggers: Mapping[str, str] = field(default_factory=dict)
    lambda_kms_key_id: str | None = None
    create_lambda_permissions: bool = True
    domain: DomainProps | None = None
    clients: tuple[ClientProps, ...] = ()
    resource_servers: tuple[ResourceServerProps, ...] = ()
    user_groups: tuple[UserGroupProps, ...] = ()
    identity_providers: tuple[IdentityProviderProps, ...] = ()
    identity_pool: IdentityPoolProps = IdentityPoolProps()
    secret: SecretProps = SecretProps()
    tags: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> UserPoolProps:
        data = schema.mapping(raw, "cognito entry", {
            "create", "name", "removal_policy", "deletion_protection", "alias_attributes",
            "username_attributes", "auto_verified_attributes", "mfa_configuration",
            "software_token_mfa_enabled", "username_configuration", "password_policy",
            "sms_configuration", "email_configuration", "admin_create_user_config",
            "device_configuration", "advanced_security_mode", "schema_attributes",
            "verification_message_template", "lambda_config", "create_lambda_permissions",
            "domain", "clients", "resource_servers", "user_groups", "identity_providers",
            "identity_pool", "secret", "tags",
        }, required=("name",))
        name = schema.name(data["name"], "cognito name")
        label = f"Cognito '{name}'"
        removal = _choice(
            data.get("removal_policy"), f"{label}.removal_policy", ("retain", "destroy"), "retain"
        )
        deletion = _choice(
            data.get("deletion_protection"), f"{label}.deletion_protection",
            ("ACTIVE", "INACTIVE"), "INACTIVE" if removal == "destroy" else "ACTIVE",
        )
        if removal == "destroy" and deletion == "ACTIVE":
            raise ValueError(
                f"{label}: removal_policy destroy cannot delete a pool with "
                "deletion_protection ACTIVE."
            )
        alias = _subset(
            data.get("alias_attributes"), f"{label}.alias_attributes",
            {"email", "phone_number", "preferred_username"}, default=None,
        )
        username = _subset(
            data.get("username_attributes"), f"{label}.username_attributes",
            {"email", "phone_number"}, default=None,
        )
        if alias and username:
            raise ValueError(
                f"{label}: alias_attributes and username_attributes are mutually exclusive."
            )
        username_config = schema.mapping(
            data.get("username_configuration") or {}, f"{label}.username_configuration",
            {"case_sensitive"},
        )
        device = schema.mapping(
            data.get("device_configuration") or {}, f"{label}.device_configuration",
            {"challenge_required_on_new_device", "device_only_remembered_on_user_prompt"},
        )
        lambda_config = schema.mapping(
            data.get("lambda_config") or {}, f"{label}.lambda_config",
            {*LAMBDA_TRIGGERS, "kms_key_id"},
        )
        triggers = {}
        for key, trigger in LAMBDA_TRIGGERS.items():
            value = lambda_config.get(key)
            if value is None:
                continue
            if isinstance(value, str) and value.startswith("arn:"):
                _arn(value, f"{label}.lambda_config.{key}", LAMBDA_ARN)
            else:
                schema.name(value, f"{label}.lambda_config.{key}")
            triggers[trigger] = value

        def entries(key: str, parse, item_key):
            return schema.items(
                data.get(key), f"{label}.{key}",
                lambda item: parse(item, f"{label}.{key}"), key=item_key,
            )

        result = cls(
            name=name,
            removal_policy=removal,
            deletion_protection=deletion,
            alias_attributes=alias,
            username_attributes=username,
            auto_verified_attributes=_subset(
                data.get("auto_verified_attributes"), f"{label}.auto_verified_attributes",
                {"email", "phone_number"}, default=("email",),
            ),
            mfa_configuration=_choice(
                data.get("mfa_configuration"), f"{label}.mfa_configuration",
                ("OFF", "ON", "OPTIONAL"), "OFF",
            ),
            software_token_mfa_enabled=schema.boolean(
                data.get("software_token_mfa_enabled"), f"{label}.software_token_mfa_enabled"
            ),
            username_case_sensitive=schema.boolean(
                username_config.get("case_sensitive"),
                f"{label}.username_configuration.case_sensitive",
            ),
            password_policy=PasswordPolicyProps.from_dict(
                data.get("password_policy"), f"{label}.password_policy"
            ),
            sms_configuration=SmsConfigurationProps.from_dict(
                data.get("sms_configuration"), f"{label}.sms_configuration"
            ),
            email_configuration=EmailConfigurationProps.from_dict(
                data.get("email_configuration"), f"{label}.email_configuration"
            ),
            admin_create_user_config=AdminCreateUserProps.from_dict(
                data.get("admin_create_user_config"), f"{label}.admin_create_user_config"
            ),
            challenge_required_on_new_device=schema.boolean(
                device.get("challenge_required_on_new_device"),
                f"{label}.device_configuration.challenge_required_on_new_device",
            ),
            device_only_remembered_on_user_prompt=schema.boolean(
                device.get("device_only_remembered_on_user_prompt"),
                f"{label}.device_configuration.device_only_remembered_on_user_prompt",
            ),
            advanced_security_mode=_choice(
                data.get("advanced_security_mode"), f"{label}.advanced_security_mode",
                ("OFF", "AUDIT", "ENFORCED"), "OFF",
            ),
            schema_attributes=entries(
                "schema_attributes", SchemaAttributeProps.from_dict, lambda item: item.name
            ),
            verification_message_template=VerificationMessageProps.from_dict(
                data.get("verification_message_template"),
                f"{label}.verification_message_template",
            ),
            lambda_triggers=triggers,
            lambda_kms_key_id=schema.string(
                lambda_config.get("kms_key_id"), f"{label}.lambda_config.kms_key_id"
            ),
            create_lambda_permissions=schema.boolean(
                data.get("create_lambda_permissions"), f"{label}.create_lambda_permissions",
                default=True,
            ),
            domain=DomainProps.from_dict(data.get("domain"), f"{label}.domain"),
            clients=entries("clients", ClientProps.from_dict, lambda item: item.name),
            resource_servers=entries(
                "resource_servers", ResourceServerProps.from_dict,
                lambda item: item.identifier,
            ),
            user_groups=entries("user_groups", UserGroupProps.from_dict, lambda item: item.name),
            identity_providers=entries(
                "identity_providers", IdentityProviderProps.from_dict,
                lambda item: item.provider_name,
            ),
            identity_pool=IdentityPoolProps.from_dict(
                data.get("identity_pool"), f"{label}.identity_pool"
            ),
            secret=SecretProps.from_dict(data.get("secret"), f"{label}.secret"),
            tags=schema.string_map(data.get("tags"), f"{label}.tags"),
        )
        result._validate(label)
        return result

    def _validate(self, label: str) -> None:
        if self.mfa_configuration != "OFF" and not (
            self.software_token_mfa_enabled or self.sms_configuration
        ):
            raise ValueError(
                f"{label}: MFA {self.mfa_configuration} needs software_token_mfa_enabled "
                "or sms_configuration."
            )
        check_unique_ids([client.name for client in self.clients], f"{label}.clients")
        providers = {"COGNITO", *(idp.provider_name for idp in self.identity_providers)}
        scopes = {
            f"{server.identifier}/{scope}"
            for server in self.resource_servers for scope, _ in server.scopes
        }
        standard = {"openid", "email", "phone", "profile", "aws.cognito.signin.user.admin"}
        for client in self.clients:
            for provider in client.supported_identity_providers:
                if provider not in providers:
                    raise ValueError(
                        f"{label}, client '{client.name}': unknown identity provider "
                        f"'{provider}'. Configured: {', '.join(sorted(providers))}."
                    )
            for scope in client.allowed_oauth_scopes:
                if scope not in standard and scope not in scopes:
                    raise ValueError(
                        f"{label}, client '{client.name}': unknown OAuth scope '{scope}'."
                    )
        if self.secret.primary_client_name is not None and self.secret.primary_client_name not in {
            client.name for client in self.clients
        }:
            raise ValueError(
                f"{label}.secret.primary_client_name: unknown client "
                f"'{self.secret.primary_client_name}'."
            )

    @property
    def secret_primary_client(self) -> str | None:
        if self.secret.primary_client_name is not None:
            return self.secret.primary_client_name
        return self.clients[0].name if self.clients else None


@dataclass(frozen=True)
class CognitoProps:
    user_pools: tuple[UserPoolProps, ...] = ()

    def __post_init__(self) -> None:
        check_unique_ids([pool.name for pool in self.user_pools], "cognito")

    def __bool__(self) -> bool:
        return bool(self.user_pools)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> CognitoProps:
        """Parse the `cognito:` list; entries with `create: false` are skipped."""
        if raw is None:
            return cls()
        if not isinstance(raw, list):
            raise ValueError("cognito must be a list.")
        enabled = []
        for item in raw:
            create = item.get("create") if isinstance(item, Mapping) else None
            if schema.boolean(create, "cognito create", default=True):
                enabled.append(item)
        return cls(user_pools=schema.items(enabled, "cognito", UserPoolProps.from_dict))
