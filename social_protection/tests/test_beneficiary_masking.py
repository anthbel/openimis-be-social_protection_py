"""Beneficiaries, their exports, and the invalid upload rows and uploaded files social_protection serves apply
individual's masks for a user without the masked-data right; filters reading a masked field are refused."""
import csv
import datetime
import io
import json
from unittest import mock

from django.urls import reverse
from rest_framework.test import APIClient

from core.models import ExportableQueryModel
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.data_masking import GroupMask, IndividualMask
from individual.models import (
    Group, GroupIndividual, Individual, IndividualDataSource, IndividualDataSourceUpload,
)
from location.test_helpers import create_test_village
from social_protection.models import Beneficiary, BenefitPlan, GroupBeneficiary
from social_protection.views import download_beneficiary_upload, download_invalid_items

MASKED = "***************"
UNMASK = "gql_query_enable_viewing_masked_data_perms"
PERMS = ["gql_beneficiary_search_perms", "gql_schema_search_perms", "gql_benefit_plan_search_perms",
         "gql_individual_search_perms", "gql_group_search_perms"]
PERSON_JSON = {"phone": "+257 1", "sex": "F"}


def benefit_plan(code, plan_type, admin):
    plan = BenefitPlan(
        code=code, name=code, type=plan_type, max_beneficiaries=0, ceiling_per_beneficiary="0.00",
        beneficiary_data_schema={"$schema": "https://json-schema.org/draft/2019-09/schema"},
        date_valid_from="2023-01-01", date_valid_to="2099-12-31")
    plan.save(user=admin)
    return plan


class BeneficiaryMaskingTest(openIMISGraphQLTestCase):
    def setUp(self):
        super().setUp()
        for mask, fields in ((IndividualMask, ["first_name", "dob", "json_ext.phone"]), (GroupMask, ["code"])):
            patcher = mock.patch.dict(mask._sources, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
            mask.register_fields("test", lambda fields=fields: list(fields))
        village = create_test_village({"name": "Beneficiary mask village", "code": "BmV"})
        district = village.parent.parent.code
        admin = create_right_only_user("bmask_admin", PERMS, [district])
        self.viewer = create_right_only_user("bmask_viewer", PERMS, [district])
        self.unmasked = create_right_only_user("bmask_unmasked", PERMS + [UNMASK], [district])
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}

        self.plan = benefit_plan("BMASK-I", BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE, admin)
        self.group_plan = benefit_plan("BMASK-G", BenefitPlan.BenefitPlanType.GROUP_TYPE, admin)
        person = Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2),
                            location=village, json_ext=dict(PERSON_JSON))
        person.save(user=admin)
        Beneficiary(individual=person, benefit_plan=self.plan, status="ACTIVE",
                    json_ext=dict(PERSON_JSON)).save(user=admin)
        group = Group(code="BMASK-G1", location=village, json_ext={})
        group.save(user=admin)
        GroupIndividual(group=group, individual=person, role=GroupIndividual.Role.HEAD).save(user=admin)
        GroupBeneficiary(group=group, benefit_plan=self.group_plan, status="ACTIVE",
                         json_ext=dict(PERSON_JSON)).save(user=admin)

        self.upload = IndividualDataSourceUpload(source_name="rows.csv", source_type="beneficiary import")
        self.upload.save(user=admin)
        IndividualDataSource(
            upload=self.upload,
            json_ext={"first_name": "Ada", "last_name": "Lovelace", "phone": "+257 1", "sex": "F"},
            validations={"validation_errors": [
                {"field_name": "phone", "note": "'phone' Field value '+257 1' is duplicated in this upload"}]},
        ).save(user=admin)

    def query_as(self, user, query):
        response = self.query(query, headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        return json.loads(response.content)

    def data_as(self, user, query):
        content = self.query_as(user, query)
        self.assertNotIn("errors", content)
        return content["data"]

    def assert_refused(self, user, query):
        content = self.query_as(user, query)
        self.assertIn("errors", content, query)
        self.assertIn("view masked data", content["errors"][0]["message"])

    def json_ext_of(self, user, query, *connections):
        """The jsonExt of the first node reached through ``connections`` from the query's data."""
        node = self.data_as(user, query)
        for connection in connections:
            node = node[connection]["edges"][0]["node"]
        return json.loads(node["jsonExt"])

    def test_beneficiary_json_ext_holds_the_person_mask_fields_masked(self):
        query = '{ beneficiary(benefitPlan_Id: "%s") { edges { node { jsonExt } } } }' % self.plan.id
        self.assertEqual(self.json_ext_of(self.viewer, query, "beneficiary"), {"phone": MASKED, "sex": "F"})
        self.assertEqual(self.json_ext_of(self.unmasked, query, "beneficiary"), PERSON_JSON)

    def test_group_beneficiary_json_ext_holds_the_head_mask_fields_masked(self):
        query = '{ groupBeneficiary(benefitPlan_Id: "%s") { edges { node { jsonExt } } } }' % self.group_plan.id
        self.assertEqual(self.json_ext_of(self.viewer, query, "groupBeneficiary"), {"phone": MASKED, "sex": "F"})
        self.assertEqual(self.json_ext_of(self.unmasked, query, "groupBeneficiary"), PERSON_JSON)

    def test_beneficiaries_reached_from_a_benefit_plan_are_masked(self):
        for plan, field in ((self.plan, "beneficiarySet"), (self.group_plan, "groupbeneficiarySet")):
            query = '{ benefitPlan(code: "%s") { edges { node { %s { edges { node { jsonExt } } } } } } }' % (
                plan.code, field)
            self.assertEqual(self.json_ext_of(self.viewer, query, "benefitPlan", field)["phone"], MASKED)
            self.assertEqual(self.json_ext_of(self.unmasked, query, "benefitPlan", field)["phone"], "+257 1")

    def test_filters_reading_a_masked_field_are_refused_without_the_masked_data_right(self):
        plan, group_plan = self.plan.id, self.group_plan.id
        refused = [
            '{ beneficiary(benefitPlan_Id: "%s", individual_FirstName_Icontains: "Ad") { totalCount } }' % plan,
            '{ beneficiary(benefitPlan_Id: "%s", search: "Ada") { totalCount } }' % plan,
            '{ beneficiary(benefitPlan_Id: "%s", customFilters: ["phone__icontains__string=257"]) '
            '{ totalCount } }' % plan,
            '{ groupBeneficiary(benefitPlan_Id: "%s", search: "Ada") { totalCount } }' % group_plan,
            '{ groupBeneficiary(benefitPlan_Id: "%s", customFilters: ["phone__icontains__string=257"]) '
            '{ totalCount } }' % group_plan,
            '{ benefitPlan(code: "BMASK-I") { edges { node { beneficiarySet(individual_FirstName_Icontains: "Ad") '
            '{ totalCount } } } } }',
        ]
        for query in refused:
            with self.subTest(query=query):
                self.assert_refused(self.viewer, query)
                self.data_as(self.unmasked, query)

    def test_filters_on_unmasked_fields_still_apply(self):
        query = '{ beneficiary(benefitPlan_Id: "%s", individual_LastName_Icontains: "Love") { totalCount } }' % (
            self.plan.id)
        self.assertEqual(self.data_as(self.viewer, query)["beneficiary"]["totalCount"], 1)

    def test_orderings_on_masked_person_fields_are_left_out(self):
        from social_protection.schema import unmasked_order_by
        order = ["headFirstName", "-group_Code", "headLastName", "dateCreated"]
        self.assertEqual(unmasked_order_by(self.viewer, GroupBeneficiary, order), ["headLastName", "dateCreated"])
        self.assertEqual(unmasked_order_by(self.viewer, Beneficiary, ["individual_FirstName", "status"]),
                         ["status"])
        self.assertEqual(unmasked_order_by(self.unmasked, GroupBeneficiary, order), order)

    def exported_cells(self, user, query):
        export = ExportableQueryModel.objects.get(name=self.data_as(user, query)["beneficiaryExport"])
        export.content.open("rb")
        rows = list(csv.DictReader(io.StringIO(export.content.read().decode())))
        self.assertTrue(rows)
        return {column: {row[column] for row in rows} for column in rows[0]}

    def test_the_export_writes_masked_columns_masked(self):
        query = ('{ beneficiaryExport(benefitPlan_Id: "%s", fields: ["id", "individual.firstName", '
                 '"individual.lastName", "jsonExt"], fieldsColumns: "{}") }' % self.plan.id)
        cells = self.exported_cells(self.viewer, query)
        self.assertEqual((cells["individual__first_name"], cells["phone"]), ({MASKED}, {MASKED}))
        self.assertEqual((cells["individual__last_name"], cells["sex"]), ({"Lovelace"}, {"F"}))
        cells = self.exported_cells(self.unmasked, query)
        self.assertEqual((cells["individual__first_name"], cells["phone"]), ({"Ada"}, {"+257 1"}))

    def test_the_export_refuses_a_filter_reading_a_masked_field(self):
        query = ('{ beneficiaryExport(benefitPlan_Id: "%s", individual_FirstName_Icontains: "Ad", '
                 'fields: ["id"], fieldsColumns: "{}") }' % self.plan.id)
        self.assert_refused(self.viewer, query)

    def download(self, user, view, **params):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(reverse(view), params)

    def test_the_invalid_rows_download_masks_the_rows(self):
        masked = b"".join(self.download(self.viewer, download_invalid_items, upload_id=self.upload.id)
                          .streaming_content).decode()
        self.assertNotIn("Ada", masked)
        self.assertNotIn("+257 1", masked)
        self.assertIn(MASKED, masked)
        self.assertIn("Lovelace", masked)
        clear = b"".join(self.download(self.unmasked, download_invalid_items, upload_id=self.upload.id)
                         .streaming_content).decode()
        self.assertIn("+257 1", clear)

    def test_the_uploaded_file_is_refused_without_the_masked_data_right(self):
        params = {"benefit_plan_id": self.plan.id, "filename": "missing.csv"}
        self.assertEqual(self.download(self.viewer, download_beneficiary_upload, **params).status_code, 403)
        self.assertNotEqual(self.download(self.unmasked, download_beneficiary_upload, **params).status_code, 403)
