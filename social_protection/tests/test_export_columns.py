"""An export column of ``beneficiaryExport`` and ``groupBeneficiaryExport`` is one of the model's own columns, one
of its ``json_ext`` keys or a relation column the frontends request; any other column is refused before any query,
naming the column."""
import csv
import io
import json

from django.core.cache import caches
from django.test import override_settings

from core.models import ExportableQueryModel
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.models import Group, GroupIndividual, Individual
from location.models import Location
from social_protection.models import Beneficiary, BenefitPlan, GroupBeneficiary
from social_protection.tests.test_beneficiary_masking import PERMS, benefit_plan


def clear_location_cache():
    caches["location"].clear()
    caches["default"].clear()


def make_location(code, loc_type, parent=None):
    return Location.objects.create(code=code, name=code, type=loc_type, parent=parent, validity_from="2019-06-01",
                                   audit_user_id=-1)


@override_settings(ROW_SECURITY=True)
class ExportColumnsTest(openIMISGraphQLTestCase):
    """Districts D and D2; the reader is limited to D2. P heads G1 in D; Q heads G2 in D2; K belongs to both.
    K is a beneficiary of the person plan, G2 of the group plan."""

    def setUp(self):
        super().setUp()
        clear_location_cache()
        self.addCleanup(clear_location_cache)
        region = make_location("SPX-R", "R")
        village = make_location("SPX-V", "V", make_location("SPX-W", "W", make_location("SPX-D", "D", region)))
        village2 = make_location("SPX-V2", "V", make_location("SPX-W2", "W", make_location("SPX-D2", "D", region)))
        clear_location_cache()
        self.admin = create_right_only_user("spx_admin", PERMS, ["SPX-D", "SPX-D2"])
        self.reader = create_right_only_user("spx_reader", PERMS, ["SPX-D2"])
        g1 = self.group("SPX-G1", village)
        self.g2 = self.group("SPX-G2", village2)
        self.join(g1, self.person("Spxpind", village), GroupIndividual.Role.HEAD)
        self.join(self.g2, self.person("Spxqind", village2), GroupIndividual.Role.HEAD)
        self.k = self.person("Spxkboth", village2)
        self.join(self.g2, self.k, GroupIndividual.Role.SON)
        self.join(g1, self.k, GroupIndividual.Role.SON)
        self.plan = benefit_plan("SPX-I", BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE, self.admin)
        self.group_plan = benefit_plan("SPX-G", BenefitPlan.BenefitPlanType.GROUP_TYPE, self.admin)
        self.beneficiary = Beneficiary(individual=self.k, benefit_plan=self.plan, status="ACTIVE",
                                       json_ext={"phone": "+257 9"})
        self.beneficiary.save(user=self.admin)
        self.group_beneficiary = GroupBeneficiary(group=self.g2, benefit_plan=self.group_plan, status="ACTIVE",
                                                  json_ext={"roof": "tin"})
        self.group_beneficiary.save(user=self.admin)
        clear_location_cache()

    def group(self, code, location):
        group = Group(code=code, location=location)
        group.save(user=self.admin)
        return group

    def person(self, last_name, location):
        person = Individual(first_name="Spx", last_name=last_name, dob="1990-01-01", location=location)
        person.save(user=self.admin)
        return person

    def join(self, group, person, role):
        GroupIndividual(group=group, individual=person, role=role).save(user=self.admin)

    def export(self, user, export, plan, fields):
        """The response of ``export`` and the rows of the files it writes."""
        token = BaseTestContext(user=user).get_jwt()
        before = set(ExportableQueryModel.objects.values_list("id", flat=True))
        response = self.query('{ %s(benefitPlan_Id: "%s", fields: %s, fieldsColumns: "{}") }'
                              % (export, plan.id, json.dumps(fields)),
                              headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        rows = []
        for file in ExportableQueryModel.objects.exclude(id__in=before):
            file.content.open("rb")
            rows += list(csv.DictReader(io.StringIO(file.content.read().decode())))
        return json.loads(response.content), rows

    REFUSED = (
        ("beneficiaryExport", "individual.groupindividuals.group.groupindividuals.individual.lastName"),
        ("beneficiaryExport", "individual__groupindividuals__group__code"),
        ("beneficiaryExport", "individual.location.individuals.lastName"),
        ("beneficiaryExport", "benefitPlan.beneficiary.individual.lastName"),
        ("beneficiaryExport", "individual.jsonExt"),
        ("groupBeneficiaryExport", "group.groupindividuals.individual.firstName"),
        ("groupBeneficiaryExport", "group.groupindividuals.individual.groupindividuals.group.code"),
        ("groupBeneficiaryExport", "group.location.groups.code"),
        ("groupBeneficiaryExport", "group.jsonExt"),
        ("groupBeneficiaryExport", "headFirstName"),
        ("groupBeneficiaryExport", "headLastName"),
        ("groupBeneficiaryExport", "headDob"),
        ("groupBeneficiaryExport", "isEligible"),
    )

    def test_an_export_column_outside_the_allowlist_is_refused_naming_the_column(self):
        from core.gql.export_mixin import ExportableQueryMixin

        for export, field in self.REFUSED:
            column = ExportableQueryMixin._adjust_notation(field)
            plan = self.plan if export == "beneficiaryExport" else self.group_plan
            for user in (self.reader, self.admin):
                with self.subTest(export=export, column=column, user=user.username):
                    files = ExportableQueryModel.objects.count()
                    content, rows = self.export(user, export, plan, ["id", "json_ext", field])
                    self.assertEqual(rows, [])
                    self.assertEqual(ExportableQueryModel.objects.count(), files)
                    self.assertIn(f"the export column {column} is not exportable", json.dumps(content["errors"]))

    def test_the_reader_gets_no_person_of_another_district_through_an_export_column(self):
        for export, field in self.REFUSED[:3] + self.REFUSED[5:7]:
            plan = self.plan if export == "beneficiaryExport" else self.group_plan
            with self.subTest(export=export, column=field):
                _content, rows = self.export(self.reader, export, plan, ["id", "json_ext", field])
                self.assertNotIn("Spxpind", json.dumps(rows))
                self.assertNotIn("SPX-G1", json.dumps(rows))

    def test_the_columns_the_frontends_request_still_export(self):
        _content, rows = self.export(self.reader, "beneficiaryExport", self.plan,
                                     ["id", "individual.first_name", "individual.last_name", "individual.dob",
                                      "json_ext", "json_ext__phone"])
        self.assertEqual([(row["id"], row["individual__last_name"], row["individual__dob"], row["phone"],
                           row["json_ext__phone"]) for row in rows],
                         [(str(self.beneficiary.id), "Spxkboth", "1990-01-01", "+257 9", "+257 9")])
        _content, rows = self.export(self.reader, "groupBeneficiaryExport", self.group_plan,
                                     ["id", "group.id", "json_ext"])
        self.assertEqual([(row["id"], row["group__id"], row["roof"]) for row in rows],
                         [(str(self.group_beneficiary.id), str(self.g2.id), "tin")])

    def test_the_model_own_columns_export(self):
        content, rows = self.export(self.reader, "beneficiaryExport", self.plan,
                                    ["id", "status", "individualId", "benefit_plan", "isDeleted", "json_ext"])
        self.assertNotIn("errors", content)
        self.assertEqual([(row["status"], row["individual_id"], row["benefit_plan"]) for row in rows],
                         [("ACTIVE", str(self.k.id), str(self.plan.id))])
