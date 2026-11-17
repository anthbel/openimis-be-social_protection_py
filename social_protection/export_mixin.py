import json
import logging
import types
from functools import partial


from core.custom_filters import CustomFilterWizardStorage
from core.models import ExportableQueryModel
from core.gql.export_mixin import ExportableQueryMixin
from individual.data_masking import check_filter_lookups, custom_filter_lookups, filter_lookups, mask_export_columns

logger = logging.getLogger(__file__)


class ExportableSocialProtectionQueryMixin(ExportableQueryMixin):
    """Exports reading the stored values, which no other mask reaches: for a user without the masked-data right,
    the filters and custom filters reading a masked field are refused, and the columns reading one are written
    masked (``mask_export_columns``, applied before the field's patches)."""

    @classmethod
    def create_export_function(cls, field_name):
        new_function_name = f"resolve_{field_name}_export"
        default_resolve = getattr(cls, F"resolve_{field_name}", None)

        if not default_resolve:
            raise AttributeError(
                f"Query {cls} doesn't provide resolve function for {field_name}. "
                f"CSV export cannot be created")

        def exporter(cls, self, info, **kwargs):
            custom_filters = kwargs.pop("customFilters", None)
            export_fields = [cls._adjust_notation(f) for f in kwargs.pop('fields')]
            fields_mapping = json.loads(kwargs.pop('fields_columns'))

            source_field = getattr(cls, field_name)
            filter_kwargs = {k: v for k, v in kwargs.items() if k in source_field.filtering_args}
            user, model = info.context.user, source_field.model
            check_filter_lookups(user, model, filter_lookups(source_field.filterset_class, filter_kwargs)
                                 + cls.__custom_filter_lookups(custom_filters, fields_mapping))

            qs = default_resolve(None, info, **kwargs)
            qs = qs.filter(**filter_kwargs)
            qs = cls.__append_custom_filters(custom_filters, qs, fields_mapping)
            patches = [partial(mask_export_columns, model, user), *(cls.get_patches_for_field(field_name) or [])]
            export_file = ExportableQueryModel\
                .create_csv_export(qs, export_fields, user, column_names=fields_mapping, patches=patches)

            return export_file.name

        setattr(cls, new_function_name, types.MethodType(exporter, cls))

    @classmethod
    def __custom_filter_lookups(cls, custom_filters, fields_mapping):
        """The model lookups of the custom filters, under the relation ``__append_custom_filters`` applies them."""
        if not custom_filters:
            return []
        relation = "group" if "group__id" in fields_mapping else cls.get_related_field()
        return custom_filter_lookups(custom_filters, relation)

    @classmethod
    def __append_custom_filters(cls, custom_filters, queryset, fields_mapping):
        if custom_filters:
            module_name = cls.get_module_name()
            object_type = cls.get_object_type()
            related_field = cls.get_related_field()
            if "group__id" in fields_mapping:
                queryset = CustomFilterWizardStorage.build_custom_filters_queryset(
                    "individual",
                    "GroupIndividual",
                    custom_filters,
                    queryset,
                    relation="group"
                )
            else:
                queryset = CustomFilterWizardStorage.build_custom_filters_queryset(
                    module_name,
                    object_type,
                    custom_filters,
                    queryset,
                    relation=related_field
                )
        return queryset
