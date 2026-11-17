from individual.data_masking import (
    GroupMask, IndividualMask, JSON_PREFIX, MASK_PLACEHOLDER, NAME_COLUMNS, RegisteredFieldsMask,
)
from social_protection.apps import SocialProtectionConfig

# The two copies of the beneficiary a maker-checker task holds in its data.
TASK_DATA_KEYS = ("incoming_data", "current_data")
# The group_beneficiary annotations reading the head's fields, with the lookups they read.
HEAD_LOOKUPS = {
    "head_first_name": "group__groupindividuals__individual__first_name",
    "head_last_name": "group__groupindividuals__individual__last_name",
    "head_dob": "group__groupindividuals__individual__dob",
}


def json_fields(fields):
    return [field for field in fields if field.startswith(JSON_PREFIX)]


def person_copy_fields():
    """The beneficiary ``json_ext`` paths holding a copy of the masked person fields: a beneficiary's
    ``json_ext`` is a copy of its person's, a group beneficiary's a copy of its head's."""
    return json_fields(IndividualMask().anon_fields)


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
    """As the beneficiary mask, and the ``json_ext`` paths of ``individual``'s group mask: a group beneficiary's
    ``json_ext`` may also be a copy of its group's (head and member names, household fields)."""

    masking_model = 'GroupBeneficiary'
    _sources = {}

    def own_fields(self):
        return SocialProtectionConfig.group_beneficiary_mask_fields or []

    @property
    def anon_fields(self):
        return list(dict.fromkeys(super().anon_fields + json_fields(GroupMask().anon_fields)))


def beneficiary_task_masker(mask_class):
    """A masker, for ``individual``'s task mask, of the maker-checker tasks of beneficiaries: both copies of the
    beneficiary in the task's data masked by ``mask_class``, and the entity string of a task on a beneficiary
    (its person's name) while a name column of the person mask is masked."""

    def mask(task):
        unit = mask_class()
        data = getattr(task, "data", None)
        for key in TASK_DATA_KEYS:
            values = data.get(key) if isinstance(data, dict) else None
            if isinstance(values, dict):
                for field in unit.anon_fields:
                    unit._anonymize_dict_field(values, field.split("."))
        names_masked = any(column in IndividualMask().anon_fields for column in NAME_COLUMNS)
        if hasattr(task, "entity_string") and mask_class is BeneficiaryMask and names_masked:
            task.entity_string = MASK_PLACEHOLDER
        return task

    return mask
