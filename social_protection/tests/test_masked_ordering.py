"""Every ordered connection of the module leaves out, for a user without the masked-data right, an orderBy term
whose path reads a masked field or traverses a JSON field, through any relation."""
import datetime
import json
from unittest import mock

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.data_masking import IndividualMask
from individual.models import Group, IndividualDataSourceUpload, Individual
from location.test_helpers import create_test_village
from social_protection.models import (
    Activity, Beneficiary, BenefitPlan, BenefitPlanDataUploadRecords, GroupBeneficiary, Project,
)

UNMASK = "gql_query_enable_viewing_masked_data_perms"
PERMS = ["gql_beneficiary_search_perms", "gql_schema_search_perms", "gql_benefit_plan_search_perms",
         "gql_individual_search_perms", "gql_group_search_perms", "gql_activity_search_perms",
         "gql_project_search_perms"]
VALUES = ("Zelda", "Abel")


class MaskedOrderingTest(openIMISGraphQLTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(IndividualMask._sources, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        IndividualMask.register_fields("test", lambda: ["first_name"])
        self.village = village = create_test_village({"name": "Ordering village", "code": "MoV"})
        district = village.parent.parent.code
        self.admin = admin = create_right_only_user("mord_admin", PERMS, [district])
        self.viewer = create_right_only_user("mord_viewer", PERMS, [district])
        self.unmasked = create_right_only_user("mord_unmasked", PERMS + [UNMASK], [district])
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}

        activity = Activity(name="MO-J activity")
        activity.save(user=admin)
        upload = IndividualDataSourceUpload(source_name="ordering.csv", source_type="beneficiary import")
        upload.save(user=admin)
        for index, value in enumerate(VALUES, 1):
            plan = self.plan(f"MO-P{index}", BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE, {"note": value})
            person = Individual(first_name=value, last_name="Ordered", dob=datetime.date(1980, 1, index),
                                location=village, json_ext={})
            person.save(user=admin)
            Beneficiary(individual=person, benefit_plan=plan, status="ACTIVE", json_ext={}).save(user=admin)
            BenefitPlanDataUploadRecords(data_upload=upload, benefit_plan=plan, workflow=f"mo-{index}",
                                         json_ext={"note": value}).save(user=admin)
            Activity(name=f"MO-A{index}", json_ext={"note": value}).save(user=admin)
            Project(name=f"MO-J{index}", benefit_plan=plan, activity=activity, location=village.parent.parent,
                    target_beneficiaries=1, working_days=1, json_ext={"note": value}).save(user=admin)

        self.person_plan = self.plan("MO-B", BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE, {})
        self.group_plan = self.plan("MO-G", BenefitPlan.BenefitPlanType.GROUP_TYPE, {})
        self.beneficiaries, self.group_beneficiaries = {}, {}
        for index, value in enumerate(VALUES, 1):
            person = Individual(first_name=f"Person {index}", last_name="Ordered", dob=datetime.date(1990, 1, index),
                                location=village, json_ext={})
            person.save(user=admin)
            beneficiary = Beneficiary(individual=person, benefit_plan=self.person_plan, status="ACTIVE",
                                      json_ext={"nickname": value})
            beneficiary.save(user=admin)
            self.beneficiaries[str(beneficiary.uuid)] = value
            group = Group(code=f"MO-G{index}", location=village, json_ext={})
            group.save(user=admin)
            group_beneficiary = GroupBeneficiary(group=group, benefit_plan=self.group_plan, status="ACTIVE",
                                                 json_ext={"roof": value})
            group_beneficiary.save(user=admin)
            self.group_beneficiaries[str(group_beneficiary.uuid)] = value

    def plan(self, code, plan_type, json_ext):
        plan = BenefitPlan(
            code=code, name=code, type=plan_type, max_beneficiaries=0, ceiling_per_beneficiary="0.00",
            beneficiary_data_schema={"$schema": "https://json-schema.org/draft/2019-09/schema"},
            date_valid_from="2023-01-01", date_valid_to="2099-12-31", json_ext=json_ext)
        plan.save(user=self.admin)
        return plan

    def ordered(self, user, connection, arguments, order, key, values):
        """The hidden values of the nodes of ``connection`` in the order the user gets them."""
        query = '{ %s(%s, orderBy: ["%s"]) { edges { node { %s } } } }' % (connection, arguments, order, key)
        response = self.query(query, headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content, query)
        return [values[edge["node"][key]] for edge in content["data"][connection]["edges"]]

    def assert_ordering_left_out(self, connection, arguments, lookup, key, values):
        ascending = self.ordered(self.viewer, connection, arguments, lookup, key, values)
        self.assertCountEqual(ascending, VALUES)
        self.assertEqual(self.ordered(self.viewer, connection, arguments, f"-{lookup}", key, values), ascending)
        self.assertEqual(self.ordered(self.unmasked, connection, arguments, lookup, key, values), ["Abel", "Zelda"])
        self.assertEqual(self.ordered(self.unmasked, connection, arguments, f"-{lookup}", key, values),
                         ["Zelda", "Abel"])

    def by_index(self, prefix):
        return {f"{prefix}{index}": value for index, value in enumerate(VALUES, 1)}

    def test_benefit_plans_by_a_masked_field_of_their_beneficiaries(self):
        self.assert_ordering_left_out("benefitPlan", 'code_Istartswith: "MO-P"',
                                      "beneficiary__individual__firstName", "code", self.by_index("MO-P"))

    def test_benefit_plan_history_by_its_json_ext(self):
        self.assert_ordering_left_out("benefitPlanHistory", 'code_Istartswith: "MO-P"', "jsonExt__note", "code",
                                      self.by_index("MO-P"))

    def test_upload_history_by_its_json_ext(self):
        self.assert_ordering_left_out("beneficiaryDataUploadHistory", 'workflow_Istartswith: "mo-"',
                                      "jsonExt__note", "workflow", self.by_index("mo-"))

    def test_activities_by_their_json_ext(self):
        self.assert_ordering_left_out("activity", 'name_Istartswith: "MO-A"', "jsonExt__note", "name",
                                      self.by_index("MO-A"))

    def test_projects_by_a_masked_field_of_their_plan_beneficiaries(self):
        self.assert_ordering_left_out("project", 'name_Istartswith: "MO-J"',
                                      "benefitPlan__beneficiary__individual__firstName", "name",
                                      self.by_index("MO-J"))

    def test_project_history_by_its_json_ext(self):
        self.assert_ordering_left_out("projectHistory", 'search: "MO-J"', "jsonExt__note", "name",
                                      self.by_index("MO-J"))

    def test_beneficiaries_by_an_unmasked_json_ext_key(self):
        self.assert_ordering_left_out("beneficiary", 'benefitPlan_Id: "%s"' % self.person_plan.id,
                                      "jsonExt__nickname", "uuid", self.beneficiaries)

    def test_group_beneficiaries_by_an_unmasked_json_ext_key(self):
        self.assert_ordering_left_out("groupBeneficiary", 'benefitPlan_Id: "%s"' % self.group_plan.id,
                                      "jsonExt__roof", "uuid", self.group_beneficiaries)

    def test_the_eligibility_annotation_still_orders(self):
        from social_protection.schema import unmasked_order_by
        self.assertEqual(unmasked_order_by(self.viewer, GroupBeneficiary, ["-isEligible"]), ["-isEligible", "id"])
