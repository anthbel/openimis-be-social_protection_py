"""The enrolment hooks: the beneficiary services call them inside their create and update, and every path that
writes enrolments without the services runs between their before_write and after_write."""
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.test_helpers import create_test_interactive_user
from individual.models import GroupDataSource, Individual, IndividualDataSource, IndividualDataSourceUpload
from social_protection import enrolment_hooks
from social_protection.apps import SocialProtectionConfig
from social_protection.models import (
    Beneficiary, BeneficiaryStatus, BenefitPlan, BenefitPlanDataUploadRecords, GroupBeneficiary,
)
from social_protection.services import BeneficiaryService, GroupBeneficiaryService
from social_protection.signals.on_confirm_enrollment_of_group import on_confirm_enrollment_of_group
from social_protection.signals.on_confirm_enrollment_of_individual import on_confirm_enrollment_of_individual
from social_protection.signals.on_validation_import_valid_items import BaseGroupColumnAggregationClass
from social_protection.tests.test_helpers import create_group_with_individual, create_individual
from social_protection.workflows.utils import SqlProcedurePythonWorkflow
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task
from tasks_management.services import TaskService

POTENTIAL = BeneficiaryStatus.POTENTIAL


class RecordingHook:
    """Records its calls; refuses every write with ``refusal`` when it is set."""

    def __init__(self):
        self.calls, self.refusal = [], None

    def before_service_write(self, model, obj_data, creating):
        self.calls.append(("service", model, creating))
        if self.refusal:
            raise ValidationError(self.refusal)

    def before_write(self, model, plan_ids):
        self.calls.append(("before", model, tuple(plan_ids)))
        return {"rows": model.objects.filter(benefit_plan_id__in=plan_ids).count()}

    def after_write(self, model, plan_ids, state):
        self.calls.append(("after", model, tuple(plan_ids), state["rows"],
                           model.objects.filter(benefit_plan_id__in=plan_ids).count()))
        if self.refusal:
            raise ValidationError(self.refusal)


class EnrolmentHooksTest(TestCase):
    def setUp(self):
        self.admin = create_test_interactive_user(username="sp_enrolment_hooks")
        self.plan = BenefitPlan(code="SPHOOKI", name="Hooks", type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE)
        self.plan.save(user=self.admin)
        self.group_plan = BenefitPlan(code="SPHOOKG", name="Hooks", type=BenefitPlan.BenefitPlanType.GROUP_TYPE)
        self.group_plan.save(user=self.admin)
        self.hook = RecordingHook()
        enrolment_hooks.register_enrolment_hook(self.hook)
        self.addCleanup(enrolment_hooks.unregister_enrolment_hook, self.hook)
        patcher = mock.patch.object(SocialProtectionConfig, "enable_maker_checker_logic_enrollment", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertWriteBetweenHooks(self, model, plan, added):
        plan_ids = (str(plan.id),)
        self.assertEqual(self.hook.calls[0], ("before", model, plan_ids))
        self.assertEqual(self.hook.calls[-1][:3], ("after", model, plan_ids))
        self.assertEqual(self.hook.calls[-1][4] - self.hook.calls[-1][3], added)

    # The services.

    def test_a_service_create_calls_the_hook_and_returns_its_refusal_as_the_error_result(self):
        individual = create_individual(self.admin.username)
        data = {"individual_id": individual.id, "benefit_plan_id": self.plan.id, "status": POTENTIAL}
        self.assertTrue(BeneficiaryService(self.admin).create(dict(data))["success"])
        self.assertEqual(self.hook.calls, [("service", Beneficiary, True)])
        self.hook.refusal = "the hook refuses"
        result = BeneficiaryService(self.admin).create(
            {**data, "individual_id": create_individual(self.admin.username).id})
        self.assertFalse(result["success"], result)
        self.assertIn("the hook refuses", result["detail"])
        self.assertEqual(Beneficiary.objects.filter(benefit_plan=self.plan).count(), 1)

    def test_a_service_update_calls_the_hook_and_returns_its_refusal_as_the_error_result(self):
        _, group, _ = create_group_with_individual(self.admin.username)
        row = GroupBeneficiary(group=group, benefit_plan=self.group_plan, status=POTENTIAL, json_ext={})
        row.save(user=self.admin)
        self.hook.refusal = "the hook refuses"
        result = GroupBeneficiaryService(self.admin).update(
            {"id": str(row.id), "benefit_plan_id": str(self.group_plan.id), "json_ext": {"note": "edited"}})
        self.assertEqual(self.hook.calls, [("service", GroupBeneficiary, False)])
        self.assertFalse(result["success"], result)
        self.assertIn("the hook refuses", result["detail"])
        self.assertEqual(GroupBeneficiary.objects.get(id=row.id).json_ext, {})

    # The paths without the services.

    def test_a_direct_confirmation_of_individuals_runs_between_the_hooks(self):
        individuals = [create_individual(self.admin.username) for _ in range(2)]
        result = {"benefit_plan_id": self.plan.id, "status": POTENTIAL, "user": self.admin,
                  "individuals_not_assigned_to_selected_programme": Individual.objects.filter(
                      id__in=[individual.id for individual in individuals])}
        on_confirm_enrollment_of_individual(result=result)
        self.assertWriteBetweenHooks(Beneficiary, self.plan, 2)

    def test_a_direct_confirmation_the_hook_refuses_enrols_nobody_and_raises(self):
        self.hook.refusal = "the hook refuses"
        _, group, _ = create_group_with_individual(self.admin.username)
        with self.assertRaisesRegex(ValidationError, "the hook refuses"):
            on_confirm_enrollment_of_group(result={
                "benefit_plan_id": self.group_plan.id, "status": POTENTIAL, "user": self.admin,
                "groups_not_assigned_to_selected_programme": type(group).objects.filter(id=group.id)})
        self.assertWriteBetweenHooks(GroupBeneficiary, self.group_plan, 1)
        self.assertFalse(GroupBeneficiary.objects.filter(benefit_plan=self.group_plan).exists())

    def enrolment_task(self, individual):
        upload = IndividualDataSourceUpload(source_name="sp-hooks", source_type="beneficiary import")
        upload.save(user=self.admin)
        record = BenefitPlanDataUploadRecords(data_upload=upload, benefit_plan=self.plan, workflow="Enrollment")
        record.save(user=self.admin)
        IndividualDataSource(upload=upload, individual=individual, json_ext={}, validations={}).save(user=self.admin)
        task = Task(source="import_valid_items", entity=record, status=Task.Status.ACCEPTED,
                    executor_action_event=TasksManagementConfig.default_executor_event,
                    business_event=SocialProtectionConfig.validation_enrollment, business_status={}, data={},
                    json_ext={"data_upload_id": str(upload.id), "benefit_plan_id": str(self.plan.id),
                              "beneficiary_status": POTENTIAL})
        task.save(user=self.admin)
        return task, upload

    def test_the_completion_of_an_enrolment_task_runs_between_the_hooks(self):
        task, _ = self.enrolment_task(create_individual(self.admin.username))
        self.assertTrue(TaskService(self.admin).complete_task({"id": task.id})["success"])
        self.assertWriteBetweenHooks(Beneficiary, self.plan, 1)

    def test_an_enrolment_task_the_hook_refuses_enrols_nobody_and_fails_its_upload(self):
        self.hook.refusal = "the hook refuses"
        task, upload = self.enrolment_task(create_individual(self.admin.username))
        self.assertTrue(TaskService(self.admin).complete_task({"id": task.id})["success"])
        self.assertFalse(Beneficiary.objects.filter(benefit_plan=self.plan).exists())
        upload.refresh_from_db()
        self.assertEqual(upload.status, IndividualDataSourceUpload.Status.FAIL)
        self.assertIn("the hook refuses", str(upload.error))

    def test_the_import_of_validated_groups_runs_between_the_hooks(self):
        _, group, _ = create_group_with_individual(self.admin.username)
        upload = IndividualDataSourceUpload(source_name="sp-hooks-groups", source_type="group import")
        upload.save(user=self.admin)
        GroupDataSource(upload=upload, json_ext={"id": str(group.id), "code": group.code}).save(user=self.admin)
        BaseGroupColumnAggregationClass.group_data_sources_into_entities(upload.id, self.admin, self.group_plan)
        self.assertWriteBetweenHooks(GroupBeneficiary, self.group_plan, 1)

    def test_the_sql_workflows_run_between_the_hooks(self):
        workflow = SqlProcedurePythonWorkflow.__new__(SqlProcedurePythonWorkflow)
        workflow.benefit_plan = self.plan
        workflow._execute_sql_logic("SELECT %s", [1])
        self.assertWriteBetweenHooks(Beneficiary, self.plan, 0)

    def test_bulk_save_runs_between_the_hooks_of_the_plans_of_its_rows(self):
        individual = create_individual(self.admin.username)
        Beneficiary.bulk_save([{"individual_id": individual.id, "benefit_plan_id": self.plan.id,
                                "status": POTENTIAL}], self.admin)
        self.assertWriteBetweenHooks(Beneficiary, self.plan, 1)
        row = Beneficiary.objects.get(benefit_plan=self.plan)
        self.hook.calls, self.hook.refusal = [], "the hook refuses"
        with self.assertRaisesRegex(ValidationError, "the hook refuses"):
            Beneficiary.bulk_save([{"id": row.id, "status": BeneficiaryStatus.ACTIVE}], self.admin)
        self.assertEqual(self.hook.calls[0], ("before", Beneficiary, (str(self.plan.id),)))
        self.assertEqual(Beneficiary.objects.get(id=row.id).status, POTENTIAL)

    def test_closing_a_plan_graduates_its_enrolments_between_the_hooks(self):
        row = Beneficiary(individual=create_individual(self.admin.username), benefit_plan=self.plan,
                          status=BeneficiaryStatus.ACTIVE, json_ext={})
        row.save(user=self.admin)
        task = Task(source="sp-hooks", business_event=SocialProtectionConfig.benefit_plan_suspend, entity=self.plan,
                    status=Task.Status.ACCEPTED, data={})
        task.save(user=self.admin)
        self.assertTrue(TaskService(self.admin).complete_task({"id": task.id})["success"])
        self.assertWriteBetweenHooks(Beneficiary, self.plan, 0)
        self.assertEqual(Beneficiary.objects.get(id=row.id).status, BeneficiaryStatus.GRADUATED)
