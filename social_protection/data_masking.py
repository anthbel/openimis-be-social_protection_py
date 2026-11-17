from individual.data_masking import IndividualMask, JSON_PREFIX, RegisteredFieldsMask
from social_protection.apps import SocialProtectionConfig


def person_copy_fields():
    """The beneficiary ``json_ext`` paths holding a copy of the masked person fields: a beneficiary's
    ``json_ext`` is a copy of its person's, a group beneficiary's a copy of its head's."""
    return [field for field in IndividualMask().anon_fields if field.startswith(JSON_PREFIX)]


class BeneficiaryMask(RegisteredFieldsMask):
    """``beneficiary_mask_fields`` while ``social_protection_masking_enabled``, the fields other modules register,
    and the copy of the masked person fields. Read each time the mask is applied."""

    masking_model = 'Beneficiary'
    _sources = {}

    @property
    def own_enabled(self):
        return bool(SocialProtectionConfig.social_protection_masking_enabled)

    def own_fields(self):
        return SocialProtectionConfig.beneficiary_mask_fields or []

    @property
    def anon_fields(self):
        return list(dict.fromkeys(super().anon_fields + person_copy_fields()))


class GroupBeneficiaryMask(BeneficiaryMask):
    masking_model = 'GroupBeneficiary'
    _sources = {}

    def own_fields(self):
        return SocialProtectionConfig.group_beneficiary_mask_fields or []
