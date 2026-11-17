"""The beneficiary and group beneficiary update mutations take an optional status: an update without one keeps the
stored status, so it never overwrites a status another writer set since the caller read the row."""
import json
from unittest import mock, skipUnless

from django.apps import apps

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_test_interactive_user
from social_protection.apps import SocialProtectionConfig
from social_protection.models import Beneficiary, BeneficiaryStatus, BenefitPlan, GroupBeneficiary
from social_protection.tests.test_helpers import create_group, create_individual
from tasks_management.models import Task
from tasks_management.services import TaskService

ACTIVE, POTENTIAL, SUSPENDED = BeneficiaryStatus.ACTIVE, BeneficiaryStatus.POTENTIAL, BeneficiaryStatus.SUSPENDED
UPDATE_MUTATIONS = {Beneficiary: "updateBeneficiary", GroupBeneficiary: "updateGroupBeneficiary"}


class BeneficiaryUpdateStatusGQLTestBase(openIMISGraphQLTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = create_test_interactive_user(username="sp_update_status_admin")
        cls.token = BaseTestContext(user=cls.admin).get_jwt()

    def setUp(self):
        super().setUp()
        self.maker_checker(False)

    def maker_checker(self, enabled):
        for name in ("gql_check_beneficiary_crud", "gql_check_group_beneficiary_crud"):
            patcher = mock.patch.object(SocialProtectionConfig, name, enabled)
            patcher.start()
            self.addCleanup(patcher.stop)

    def plan(self, model, cap=None):
        plan_type = (BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE if model is Beneficiary
                     else BenefitPlan.BenefitPlanType.GROUP_TYPE)
        plan = BenefitPlan(code=f"SP47{model.__name__[:3]}{BenefitPlan.objects.count()}", name="Status",
                           type=plan_type, max_beneficiaries=cap)
        plan.save(user=self.admin)
        return plan

    def enrolment(self, model, plan, status, json_ext=None):
        if model is Beneficiary:
            row = Beneficiary(individual=create_individual(self.admin.username), benefit_plan=plan, status=status,
                              json_ext=json_ext or {})
        else:
            row = GroupBeneficiary(group=create_group(self.admin.username), benefit_plan=plan, status=status,
                                   json_ext=json_ext or {})
        row.save(user=self.admin)
        return row

    def update(self, model, row, **fields):
        """Sends the update mutation of ``model`` for ``row`` with ``fields`` (GraphQL literals) and returns its
        mutation log node."""
        inputs = {"id": f'"{row.id}"', "benefitPlanId": f'"{row.benefit_plan_id}"', **fields}
        mutation = UPDATE_MUTATIONS[model]
        response = self.query(
            f"mutation {{ {mutation}(input: {{ {' '.join(f'{k}: {v}' for k, v in inputs.items())} }}) "
            f"{{ internalId }} }}",
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.token}"})
        self.assertResponseNoErrors(response)
        internal_id = json.loads(response.content)["data"][mutation]["internalId"]
        return self._get_mutation_log_node(internal_id, self.token)

    @staticmethod
    def json_literal(value):
        return json.dumps(json.dumps(value))


class BeneficiaryUpdateInputTest(BeneficiaryUpdateStatusGQLTestBase):
    def input_field_types(self, type_name):
        response = self.query(
            f'{{ __type(name: "{type_name}") {{ inputFields {{ name type {{ kind }} }} }} }}',
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.token}"})
        self.assertResponseNoErrors(response)
        return {field["name"]: field["type"]["kind"]
                for field in json.loads(response.content)["data"]["__type"]["inputFields"]}

    def test_the_update_inputs_take_an_optional_status_and_the_create_inputs_require_one(self):
        for type_name in ("UpdateBeneficiaryMutationInput", "UpdateGroupBeneficiaryMutationInput"):
            self.assertEqual(self.input_field_types(type_name)["status"], "ENUM", type_name)
        for type_name in ("CreateBeneficiaryMutationInput", "CreateGroupBeneficiaryMutationInput"):
            self.assertEqual(self.input_field_types(type_name)["status"], "NON_NULL", type_name)

    def test_an_update_without_status_keeps_the_stored_status_and_writes_the_rest(self):
        for model in (Beneficiary, GroupBeneficiary):
            with self.subTest(model=model.__name__):
                row = self.enrolment(model, self.plan(model), ACTIVE, {"k": "v"})
                node = self.update(model, row, jsonExt=self.json_literal({"k": "w"}))
                self.assertEqual(node["status"], 2, node["error"])
                row.refresh_from_db()
                self.assertEqual(row.status, ACTIVE)
                self.assertEqual(row.json_ext, {"k": "w"})

    def test_an_update_with_a_null_status_keeps_the_stored_status(self):
        for model in (Beneficiary, GroupBeneficiary):
            with self.subTest(model=model.__name__):
                row = self.enrolment(model, self.plan(model), POTENTIAL)
                mutation = UPDATE_MUTATIONS[model]
                input_type = f"{mutation[0].upper()}{mutation[1:]}MutationInput"
                response = self.query(
                    f"mutation ($input: {input_type}!) {{ {mutation}(input: $input) {{ internalId }} }}",
                    variables={"input": {"id": str(row.id), "benefitPlanId": str(row.benefit_plan_id),
                                         "status": None}},
                    headers={"HTTP_AUTHORIZATION": f"Bearer {self.token}"})
                self.assertResponseNoErrors(response)
                node = self._get_mutation_log_node(
                    json.loads(response.content)["data"][mutation]["internalId"], self.token)
                self.assertEqual(node["status"], 2, node["error"])
                row.refresh_from_db()
                self.assertEqual(row.status, POTENTIAL)

    def test_an_update_task_without_status_neither_compares_nor_writes_the_status(self):
        self.maker_checker(True)
        for model in (Beneficiary, GroupBeneficiary):
            with self.subTest(model=model.__name__):
                row = self.enrolment(model, self.plan(model), POTENTIAL, {"k": "v"})
                node = self.update(model, row, jsonExt=self.json_literal({"k": "w"}))
                self.assertEqual(node["status"], 2, node["error"])
                task = Task.objects.get(entity_id=str(row.id), business_event__endswith=".update")
                self.assertNotIn("status", task.data["incoming_data"])
                self.assertNotIn("status", task.data["current_data"])
                row.status = ACTIVE
                row.save(user=self.admin)
                task.status = Task.Status.ACCEPTED
                task.save(user=self.admin)
                self.assertTrue(TaskService(self.admin).complete_task({"id": str(task.id)})["success"])
                task.refresh_from_db()
                self.assertEqual(task.status, Task.Status.COMPLETED, task.json_ext)
                row.refresh_from_db()
                self.assertEqual(row.status, ACTIVE)
                self.assertEqual(row.json_ext, {"k": "w"})


@skipUnless(apps.is_installed("programme_targeting"), "programme_targeting registers the enrolment rules")
class BeneficiaryUpdateUnderTheEnrolmentRulesTest(BeneficiaryUpdateStatusGQLTestBase):
    """Direct updates of a row selection suspended, under programme_targeting's enrolment rules."""

    def setUp(self):
        super().setUp()
        from programme_targeting.enrolment_rules import SUSPENSION_KEY
        self.key = SUSPENSION_KEY

    def suspended(self, model, cap=None):
        plan = self.plan(model, cap)
        return plan, self.enrolment(model, plan, SUSPENDED, {self.key: "request-1", "k": "v"})

    def test_an_edit_without_status_keeps_the_selection_suspension_and_its_key(self):
        for model in (Beneficiary, GroupBeneficiary):
            for fields in ({"jsonExt": self.json_literal({"k": "w"})}, {"dateValidTo": '"2030-01-01"'}):
                with self.subTest(model=model.__name__, fields=sorted(fields)):
                    _, row = self.suspended(model)
                    node = self.update(model, row, **fields)
                    self.assertEqual(node["status"], 2, node["error"])
                    row.refresh_from_db()
                    self.assertEqual(row.status, SUSPENDED)
                    self.assertEqual(row.json_ext[self.key], "request-1")

    def test_an_edit_without_status_and_with_a_json_ext_without_the_key_keeps_the_key(self):
        for model in (Beneficiary, GroupBeneficiary):
            with self.subTest(model=model.__name__):
                _, row = self.suspended(model)
                self.update(model, row, jsonExt=self.json_literal({"k": "w"}))
                row.refresh_from_db()
                self.assertEqual(row.json_ext, {"k": "w", self.key: "request-1"})

    def test_an_explicit_activation_under_the_cap_activates_and_removes_the_key(self):
        for model in (Beneficiary, GroupBeneficiary):
            with self.subTest(model=model.__name__):
                _, row = self.suspended(model, cap=1)
                node = self.update(model, row, status=ACTIVE)
                self.assertEqual(node["status"], 2, node["error"])
                row.refresh_from_db()
                self.assertEqual(row.status, ACTIVE)
                self.assertEqual(row.json_ext, {"k": "v"})

    def test_an_explicit_activation_at_the_cap_is_refused(self):
        for model in (Beneficiary, GroupBeneficiary):
            with self.subTest(model=model.__name__):
                plan, row = self.suspended(model, cap=1)
                self.enrolment(model, plan, ACTIVE)
                node = self.update(model, row, status=ACTIVE)
                self.assertEqual(node["status"], 1, node)
                self.assertIn("max active beneficiaries", node["error"])
                row.refresh_from_db()
                self.assertEqual(row.status, SUSPENDED)
                self.assertEqual(row.json_ext[self.key], "request-1")
