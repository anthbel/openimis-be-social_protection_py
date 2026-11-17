"""The task calcrule_social_protection creates for a payment exceeding the transaction limit holds, in its data, the
bill of the beneficiary: its terms and line code carry the person's names or the group's code. For a reader without
the masked-data right they are masked while the person mask masks a name column, or the group mask the code."""
import ast
import datetime
import json
from unittest import mock

from calcrule_social_protection.calculation_rule import SocialProtectionCalculationRule
from calcrule_social_protection.strategies.benefit_package_group_strategy import GroupBenefitPackageStrategy
from calcrule_social_protection.strategies.benefit_package_individual_strategy import (
    IndividualBenefitPackageStrategy,
)
from contribution_plan.models import PaymentPlan
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.data_masking import GroupIndividualMask, GroupMask, IndividualMask
from individual.models import Group, GroupIndividual, Individual
from location.test_helpers import create_test_village
from payment_cycle.models import PaymentCycle
from payroll.models import Payroll
from social_protection.models import Beneficiary, BenefitPlan, GroupBeneficiary
from social_protection.tests.test_beneficiary_masking import MASKED, UNMASK, benefit_plan
from tasks_management.models import Task, TaskExecutor, TaskGroup

PERMS = ["gql_task_search_perms"]
SOURCE = "calcrule_social_protection"
TASK_QUERY = ('{ task(source: "%s") { edges { node { data } } } '
              'taskHistory(source: "%s") { edges { node { data } } } }' % (SOURCE, SOURCE))


class CalcruleTaskMaskingTest(openIMISGraphQLTestCase):
    def setUp(self):
        super().setUp()
        for mask, fields in ((IndividualMask, ["first_name"]), (GroupMask, ["code"]), (GroupIndividualMask, [])):
            patcher = mock.patch.dict(mask._sources, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
            mask.register_fields("test", lambda fields=fields: list(fields))
        village = create_test_village({"name": "Calcrule mask village", "code": "CmV"})
        district = village.parent.parent.code
        self.admin = admin = create_right_only_user("cmask_admin", PERMS + [UNMASK], [district])
        self.viewer = create_right_only_user("cmask_viewer", PERMS, [district])
        self.unmasked = create_right_only_user("cmask_unmasked", PERMS + [UNMASK], [district])
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}
        person = Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2), location=village)
        person.save(user=admin)
        group = Group(code="CMASK-G1", location=village, json_ext={})
        group.save(user=admin)
        GroupIndividual(group=group, individual=person, role=GroupIndividual.Role.HEAD).save(user=admin)
        plan = benefit_plan("CMASK-I", BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE, admin)
        group_plan = benefit_plan("CMASK-G", BenefitPlan.BenefitPlanType.GROUP_TYPE, admin)
        beneficiary = Beneficiary(individual=person, benefit_plan=plan, status="ACTIVE", json_ext={})
        beneficiary.save(user=admin)
        group_beneficiary = GroupBeneficiary(group=group, benefit_plan=group_plan, status="ACTIVE", json_ext={})
        group_beneficiary.save(user=admin)
        cycle = PaymentCycle(code="CMASK-C", start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
                             status=PaymentCycle.PaymentCycleStatus.ACTIVE)
        cycle.save(user=admin)
        self.task_group = TaskGroup(code="cmask_tasks", completion_policy="ANY")
        self.task_group.save(user=admin)
        for user in (self.viewer, self.unmasked):
            TaskExecutor(task_group=self.task_group, user=user).save(user=admin)
        for strategy, unit_plan, entity in ((IndividualBenefitPackageStrategy, plan, {"beneficiary": beneficiary}),
                                            (GroupBenefitPackageStrategy, group_plan, {"group": group_beneficiary})):
            self.limit_exceeded(strategy, unit_plan, cycle, entity)

    def limit_exceeded(self, strategy, plan, cycle, entity):
        """Runs calcrule's conversion of a payment exceeding the limit, which creates the task, then accepts it into
        the readers' task group."""
        payment_plan = PaymentPlan(code=f"{plan.code}-PP", name=plan.code, benefit_plan=plan, periodicity=1,
                                   calculation=SocialProtectionCalculationRule.uuid, json_ext={})
        payment_plan.save(user=self.admin)
        payroll = Payroll(name=f"{plan.code}-P", payment_plan=payment_plan, payment_cycle=cycle)
        payroll.save(user=self.admin)
        tasks = set(Task.objects.values_list("id", flat=True))
        strategy.convert(payment_plan, amount=150.0, user=self.admin, end_date=cycle.end_date, payment_cycle=cycle,
                         payroll=payroll, is_exceed_limit=True, **entity)
        [task] = Task.objects.exclude(id__in=tasks)
        task.status = Task.Status.ACCEPTED
        task.task_group = self.task_group
        task.save(user=self.admin)

    def bills(self, user):
        """The bill of each task and task history record ``user`` reads, by the unit's code or name it carries."""
        response = self.query(TASK_QUERY, headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        nodes = [edge["node"] for name in ("task", "taskHistory") for edge in content["data"][name]["edges"]]
        self.assertGreaterEqual(len(nodes), 4)
        return [ast.literal_eval(json.loads(node["data"])) for node in nodes]

    def test_the_names_and_group_codes_are_masked_without_the_masked_data_right(self):
        for results in self.bills(self.viewer):
            self.assertEqual(results["bill_data"]["terms"], MASKED)
            self.assertEqual([line["code"] for line in results["bill_data_line"]], [MASKED])
            self.assertEqual(results["bill_data"]["amount_net"], 150.0)
            self.assertEqual(results["benefit"]["benefit_data"]["amount"], 150.0)

    def test_the_bill_is_clear_with_the_masked_data_right(self):
        codes = sorted(line["code"] for results in self.bills(self.unmasked) for line in results["bill_data_line"])
        self.assertEqual(set(codes), {"Ada-Lovelace", "CMASK-G1"})

    def test_an_unmasked_unit_is_clear(self):
        with mock.patch.dict(GroupMask._sources, {"test": lambda: []}):
            codes = {line["code"] for results in self.bills(self.viewer) for line in results["bill_data_line"]}
        self.assertEqual(codes, {MASKED, "CMASK-G1"})

    def test_data_that_cannot_be_read_is_masked_whole(self):
        Task.objects.filter(source=SOURCE).update(data="{'bill_data': Decimal('1')} Ada-Lovelace")
        response = self.query('{ task(source: "%s") { edges { node { data } } } }' % SOURCE,
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[self.viewer.username]}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        data = [json.loads(edge["node"]["data"]) for edge in content["data"]["task"]["edges"]]
        self.assertEqual(data, [MASKED, MASKED])
