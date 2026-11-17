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
from social_protection.services import BeneficiaryService, GroupBeneficiaryService
from tasks_management.models import Task, TaskExecutor, TaskGroup
from social_protection.views import (
    download_beneficiary_upload, download_invalid_items, validate_import_beneficiaries,
)

MASKED = "***************"
UNMASK = "gql_query_enable_viewing_masked_data_perms"
PERMS = ["gql_beneficiary_search_perms", "gql_schema_search_perms", "gql_benefit_plan_search_perms",
         "gql_individual_search_perms", "gql_group_search_perms", "gql_task_search_perms",
         "gql_individual_create_perms"]
PERSON_JSON = {"phone": "+257 1", "sex": "F"}
# A group beneficiary's json_ext: its head's copy and its group's copy (head and member names, household fields).
GROUP_JSON = {"phone": "+257 1", "sex": "F", "head": "Ada Lovelace", "members": {"m1": "Ada Lovelace"},
              "hh_secret": "HS-1", "roof": "tin"}
GROUP_JSON_MASKED = {"phone": MASKED, "sex": "F", "head": MASKED, "members": {"m1": MASKED},
                     "hh_secret": MASKED, "roof": "tin"}


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
        for mask, fields in ((IndividualMask, ["first_name", "dob", "json_ext.phone"]), (GroupMask, ["code", "json_ext.hh_secret"])):
            patcher = mock.patch.dict(mask._sources, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
            mask.register_fields("test", lambda fields=fields: list(fields))
        village = create_test_village({"name": "Beneficiary mask village", "code": "BmV"})
        district = village.parent.parent.code
        self.admin = admin = create_right_only_user("bmask_admin", PERMS, [district])
        self.viewer = create_right_only_user("bmask_viewer", PERMS, [district])
        self.unmasked = create_right_only_user("bmask_unmasked", PERMS + [UNMASK], [district])
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}

        self.plan = benefit_plan("BMASK-I", BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE, admin)
        self.group_plan = benefit_plan("BMASK-G", BenefitPlan.BenefitPlanType.GROUP_TYPE, admin)
        self.person = person = Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2),
                                          location=village, json_ext=dict(PERSON_JSON))
        person.save(user=admin)
        self.beneficiary = Beneficiary(individual=person, benefit_plan=self.plan, status="ACTIVE",
                                       json_ext=dict(PERSON_JSON))
        self.beneficiary.save(user=admin)
        self.group = group = Group(code="BMASK-G1", location=village, json_ext={})
        group.save(user=admin)
        GroupIndividual(group=group, individual=person, role=GroupIndividual.Role.HEAD).save(user=admin)
        self.group_beneficiary = GroupBeneficiary(group=group, benefit_plan=self.group_plan, status="ACTIVE",
                                                  json_ext=json.loads(json.dumps(GROUP_JSON)))
        self.group_beneficiary.save(user=admin)

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
        self.assertEqual(self.json_ext_of(self.viewer, query, "groupBeneficiary"), GROUP_JSON_MASKED)
        self.assertEqual(self.json_ext_of(self.unmasked, query, "groupBeneficiary"), GROUP_JSON)

    def test_beneficiaries_reached_from_a_benefit_plan_are_masked(self):
        for plan, field in ((self.plan, "beneficiarySet"), (self.group_plan, "groupbeneficiarySet")):
            query = '{ benefitPlan(code: "%s") { edges { node { %s { edges { node { jsonExt } } } } } } }' % (
                plan.code, field)
            self.assertEqual(self.json_ext_of(self.viewer, query, "benefitPlan", field)["phone"], MASKED)
            self.assertEqual(self.json_ext_of(self.unmasked, query, "benefitPlan", field)["phone"], "+257 1")
        query = ('{ benefitPlan(code: "%s") { edges { node { groupbeneficiarySet { edges { node { jsonExt } } } } } } }'
                 % self.group_plan.code)
        self.assertEqual(self.json_ext_of(self.viewer, query, "benefitPlan", "groupbeneficiarySet"), GROUP_JSON_MASKED)

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
        self.assertEqual(unmasked_order_by(self.viewer, GroupBeneficiary, order),
                         ["headLastName", "dateCreated", "id"])
        self.assertEqual(unmasked_order_by(self.viewer, Beneficiary, ["individual_FirstName", "status"]),
                         ["status", "id"])
        self.assertEqual(unmasked_order_by(self.viewer, GroupBeneficiary, ["headFirstName"]), ["id"])
        self.assertEqual(unmasked_order_by(self.unmasked, GroupBeneficiary, order), [*order, "id"])

    def test_group_beneficiaries_ordered_by_a_masked_head_field_come_in_primary_key_order(self):
        second = Group(code="BMASK-G2", location=self.person.location, json_ext={})
        second.save(user=self.admin)
        head = Individual(first_name="Zoe", last_name="Ziegler", dob=datetime.date(1990, 1, 1),
                          location=self.person.location, json_ext={})
        head.save(user=self.admin)
        GroupIndividual(group=second, individual=head, role=GroupIndividual.Role.HEAD).save(user=self.admin)
        other = GroupBeneficiary(group=second, benefit_plan=self.group_plan, status="ACTIVE", json_ext={})
        other.save(user=self.admin)
        by_id = sorted([self.group_beneficiary, other], key=lambda beneficiary: str(beneficiary.id))
        first_name = {self.group_beneficiary.id: "Ada", other.id: "Zoe"}
        descending = "-" if first_name[by_id[0].id] < first_name[by_id[1].id] else ""
        query = ('{ groupBeneficiary(benefitPlan_Id: "%s", orderBy: ["%sheadFirstName"]) { edges { node { uuid } } } }'
                 % (self.group_plan.id, descending))
        names = {str(beneficiary.uuid): first_name[beneficiary.id] for beneficiary in by_id}
        order = [names[edge["node"]["uuid"]] for edge in self.data_as(self.viewer, query)["groupBeneficiary"]["edges"]]
        self.assertEqual(order, [first_name[beneficiary.id] for beneficiary in by_id])
        order = [names[edge["node"]["uuid"]] for edge in self.data_as(self.unmasked, query)["groupBeneficiary"]["edges"]]
        self.assertEqual(order, sorted(names.values(), reverse=bool(descending)))

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

    def group_exported_cells(self, user, fields):
        query = ('{ groupBeneficiaryExport(benefitPlan_Id: "%s", fields: [%s], fieldsColumns: "{}") }'
                 % (self.group_plan.id, ", ".join(f'"{field}"' for field in fields)))
        export = ExportableQueryModel.objects.get(name=self.data_as(user, query)["groupBeneficiaryExport"])
        export.content.open("rb")
        rows = list(csv.DictReader(io.StringIO(export.content.read().decode())))
        self.assertTrue(rows)
        return {column: {row[column] for row in rows} for column in rows[0]}

    def test_the_group_export_writes_the_head_columns_masked(self):
        fields = ["id", "jsonExt", "headFirstName", "headLastName", "headDob"]
        cells = self.group_exported_cells(self.viewer, fields)
        self.assertEqual((cells["head_first_name"], cells["head_dob"], cells["head_last_name"]),
                         ({MASKED}, {MASKED}, {"Lovelace"}))
        cells = self.group_exported_cells(
            self.viewer, [*fields, "group.groupindividuals.individual.firstName"])
        self.assertEqual((cells["head_first_name"], cells["group__groupindividuals__individual__first_name"]),
                         ({MASKED}, {MASKED}))
        cells = self.group_exported_cells(self.unmasked, fields)
        self.assertEqual((cells["head_first_name"], cells["head_dob"]), ({"Ada"}, {"1980-01-02"}))

    def test_the_group_export_writes_the_group_copy_masked(self):
        cells = self.group_exported_cells(self.viewer, ["id", "jsonExt"])
        self.assertEqual((cells["head"], cells["hh_secret"], cells["phone"], cells["roof"]),
                         ({MASKED}, {MASKED}, {MASKED}, {"tin"}))
        self.assertEqual(cells["members.m1"], {MASKED})
        cells = self.group_exported_cells(self.unmasked, ["id", "jsonExt"])
        self.assertEqual((cells["head"], cells["hh_secret"]), ({"Ada Lovelace"}, {"HS-1"}))

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

    def accept_task(self, result):
        self.assertTrue(result["success"], result)
        task = Task.objects.get(id=result["data"]["id"])
        task.status = Task.Status.ACCEPTED
        task.task_group = self.task_group
        task.save(user=self.admin)

    def beneficiary_tasks(self):
        self.task_group = TaskGroup(code="bmask_tasks", completion_policy="ANY")
        self.task_group.save(user=self.admin)
        for user in (self.viewer, self.unmasked):
            TaskExecutor(task_group=self.task_group, user=user).save(user=self.admin)
        self.accept_task(BeneficiaryService(self.admin).create_update_task({
            "id": str(self.beneficiary.id), "benefit_plan_id": str(self.plan.id), "status": "ACTIVE",
            "individual_id": str(self.person.id), "json_ext": {"phone": "+257 9", "sex": "F"}}))
        self.accept_task(GroupBeneficiaryService(self.admin).create_update_task({
            "id": str(self.group_beneficiary.id), "benefit_plan_id": str(self.group_plan.id), "status": "ACTIVE",
            "group_id": str(self.group.id), "json_ext": {**GROUP_JSON, "phone": "+257 8", "hh_secret": "HS-2"}}))

    def task_nodes(self, user, source):
        query = ('{ task(source: "%s") { edges { node { entityString data businessData } } } '
                 'taskHistory(source: "%s") { edges { node { entityString data } } } }' % (source, source))
        data = self.data_as(user, query)
        nodes = [edge["node"] for edge in data["task"]["edges"] + data["taskHistory"]["edges"]]
        self.assertTrue(data["taskHistory"]["edges"])
        return nodes

    def test_beneficiary_tasks_are_masked_without_the_masked_data_right(self):
        self.beneficiary_tasks()
        for node in self.task_nodes(self.viewer, "BeneficiaryService"):
            self.assertEqual(node["entityString"], MASKED)
            data = json.loads(node["data"])
            self.assertEqual(data["incoming_data"]["json_ext"], {"phone": MASKED, "sex": "F"})
            self.assertEqual(data["current_data"]["json_ext"], {"phone": MASKED, "sex": "F"})
            if node.get("businessData"):
                business = json.loads(node["businessData"])
                self.assertEqual(business["incoming_data"]["id"], f"{MASKED} Lovelace")
                self.assertEqual(business["incoming_data"]["individual_id"], f"{MASKED} Lovelace")
                self.assertEqual(business["incoming_data"]["json_ext"]["phone"], MASKED)

    def test_group_beneficiary_tasks_are_masked_without_the_masked_data_right(self):
        self.beneficiary_tasks()
        for node in self.task_nodes(self.viewer, "GroupBeneficiaryService"):
            data = json.loads(node["data"])
            self.assertEqual(data["incoming_data"]["json_ext"], GROUP_JSON_MASKED)
            self.assertEqual(data["current_data"]["json_ext"], GROUP_JSON_MASKED)

    def test_beneficiary_tasks_are_clear_with_the_masked_data_right(self):
        self.beneficiary_tasks()
        for node in self.task_nodes(self.unmasked, "BeneficiaryService"):
            self.assertEqual(node["entityString"], "Ada Lovelace")
            self.assertEqual(json.loads(node["data"])["incoming_data"]["json_ext"]["phone"], "+257 9")
            if node.get("businessData"):
                self.assertEqual(json.loads(node["businessData"])["incoming_data"]["id"], "Ada Lovelace")
        for node in self.task_nodes(self.unmasked, "GroupBeneficiaryService"):
            self.assertEqual(json.loads(node["data"])["current_data"]["json_ext"], GROUP_JSON)

    def validate_upload(self, user, upload):
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(reverse(validate_import_beneficiaries),
                                {"benefit_plan": str(self.plan.uuid), "upload_id": str(upload.id)}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return [item["row"] for item in response.json()["data"]]

    def own_upload(self, user):
        upload = IndividualDataSourceUpload(source_name="own.csv", source_type="beneficiary import")
        upload.save(user=user)
        IndividualDataSource(upload=upload, json_ext={"first_name": "Ada", "last_name": "Lovelace",
                                                      "phone": "+257 1", "sex": "F"}).save(user=user)
        return upload

    def test_validating_an_upload_gives_its_rows_masked_without_the_masked_data_right(self):
        row, = self.validate_upload(self.viewer, self.own_upload(self.viewer))
        self.assertEqual((row["first_name"], row["phone"], row["last_name"]), (MASKED, MASKED, "Lovelace"))
        row, = self.validate_upload(self.unmasked, self.own_upload(self.unmasked))
        self.assertEqual((row["first_name"], row["phone"]), ("Ada", "+257 1"))

    def test_validating_an_upload_of_another_user_gives_no_row(self):
        self.assertEqual(self.validate_upload(self.viewer, self.upload), [])
