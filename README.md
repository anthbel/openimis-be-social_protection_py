# openIMIS Backend social_protection reference module
This repository holds the files of the openIMIS Backend social_protection reference module.
It is dedicated to be deployed as a module of [openimis-be_py](https://github.com/openimis/openimis-be_py).

## ORM mapping:
* social_protection_benefitplan, social_protection_historicalbenefitplan > BenefitPlan
* social_protection_beneficiary, social_protection_historicalbeneficiary > Beneficiary
* social_protection_benefitplandatauploadsrecords, social_protection_historicalbenefitplandatauploadsrecords > BenefitPlanDataUploadRecords
* social_protection_groupbeneficiary, social_protection_historicalgroupbeneficiary > GroupBeneficiary

## GraphQl Queries
* benefitPlan
* beneficiary
* groupBeneficiary
* beneficiaryDataUploadHistory
* bfCodeValidity
* bfNameValidity
* bfNameValidity
* bfSchemaValidity
* beneficiaryExport
* groupBeneficiaryExport

## GraphQL Mutations - each mutation emits default signals and return standard error lists (cfr. openimis-be-core_py)
* createBenefitPlan
* updateBenefitPlan
* deleteBenefitPlan
* createBeneficiary
* updateBeneficiary
* deleteBeneficiary
* createGroupBeneficiary
* updateGroupBeneficiary
* deleteGroupBeeficiary

`createBeneficiary` and `createGroupBeneficiary` require `status`. In `updateBeneficiary` and `updateGroupBeneficiary`
it is optional: an update without `status`, or with a null one, keeps the stored status and sends none to the service
or to the maker-checker task, so the enrolment hooks see the write as one that does not change the status.

## Services
- BenefitPlan
  - create
  - update
  - delete
  - create_update_task
- Beneficiary
  - create
  - update
  - delete
- GroupBeneficiary
  - create
  - update
  - delete
- BeneficiaryImport
  - import_beneficiaries

## Enrolment hooks
Another module checks every write of enrolments (`Beneficiary`, `GroupBeneficiary`) by registering a hook with
`social_protection.enrolment_hooks.register_enrolment_hook(hook)`, usually in its `AppConfig.ready()`. With no hook
registered, each of these writers still runs in a transaction of its own: a failure part way rolls back the
whole write (a confirmation, an enrolment task, an import, a workflow run, a `bulk_save` call), which then writes
nothing.
- `hook.before_service_write(model, obj_data, creating)`: `BeneficiaryService` and `GroupBeneficiaryService` call
  it in the transaction of their `create` and `update`, before the row is read. It may adjust `obj_data`; a
  `ValidationError` it raises is returned as the service's error result (`{'success': False, ...}`).
- `hook.before_write(model, plan_ids)` and `hook.after_write(model, plan_ids, state)`: the writers of enrolments
  without the services run in a transaction between them (`enrolment_hooks.enrolment_write`): the direct
  confirmation of an enrolment (`on_confirm_enrollment_of_group`, `on_confirm_enrollment_of_individual`), the
  completion of an enrolment task and the import of validated group items (`on_validation_import_valid_items.py`),
  the SQL upload and update workflows (`SqlProcedurePythonWorkflow`), the closing of a benefit plan, and
  `Beneficiary.bulk_save` / `GroupBeneficiary.bulk_save`. A `ValidationError` raised by `after_write` rolls the
  write back and reaches the writer's caller: the caller of `select_groups_to_benefit_plan` /
  `select_individuals_to_benefit_plan` (the confirmation mutation), the upload of an enrolment task, a group
  import or an SQL workflow (set `FAIL` with the message), or the caller of `bulk_save`.

## Configuration options (can be changed via core.ModuleConfiguration)
* gql_benefit_plan_search_perms: required rights to call benefitPlan GraphQL Query (default: ["160001"])
* gql_benefit_plan_create_perms: required rights to call createBenefitPlan GraphQL Mutation (default: ["160002"])
* gql_benefit_plan_update_perms: required rights to call updateBenefitPlan GraphQL Mutation (default: ["160003"])
* gql_benefit_plan_delete_perms: required rights to call deleteBenefitPlan GraphQL Mutation (default: ["160004"])
* gql_beneficiary_search_perms: required rights to call beneficiary and groupBeneficiary GraphQL Mutation (default: ["170001"])
* gql_beneficiary_create_perms: required rights to call createBeneficiary and createGroupBeneficiary GraphQL Mutation (default: ["160002"])
* gql_beneficiary_update_perms: required rights to call updateBeneficiary and updateGroupBeneficiary GraphQL Mutation (default: ["160003"])
* gql_beneficiary_delete_perms: required rights to call deleteBeneficiary and deleteGroupBeneficiary GraphQL Mutation (default: ["170004"])

* gql_check_benefit_plan_update: specifies whether Benefit Plan update should be updated using task based approval (default: True)
* gql_check_beneficiary_crud: specifies whether Beneficiary CRUD should be use task based approval (default: True)
* gql_check_group_beneficiary_crud: specifies whether Group Beneficiary should use tasks based approval (default: True),


## Data masking
For a user without the masked-data right (core `gql_query_enable_viewing_masked_data_perms`, 900101):
* `Beneficiary` and `GroupBeneficiary` are masked by `beneficiary_mask_fields` and `group_beneficiary_mask_fields` (while
  `social_protection_masking_enabled`), the fields other modules register with `register_fields`, and the `json_ext`
  paths of `individual`'s person mask: a beneficiary's `json_ext` is a copy of its person's, a group beneficiary's a copy
  of its head's. A group beneficiary's `json_ext` may also be a copy of its group's, so the `json_ext` paths of
  `individual`'s group mask (head and member names, registered household fields) are masked too. The fields are read
  each time a mask is applied, so a reloaded configuration applies.
* The maker-checker tasks of beneficiaries and group beneficiaries (sources `BeneficiaryService`,
  `GroupBeneficiaryService`) are masked through `individual`'s task mask (`TaskMask.register_source`): both copies of
  the beneficiary in the task's `data` by the beneficiary or group beneficiary mask, the entity string of a task on a
  beneficiary (its person's name) while a name column is masked, on the task, its history and every path reaching
  them. Their business data writes the person's name with each masked name column shown as the mask.
* The tasks `calcrule_social_protection` creates for a payment above the limit per transaction (source
  `calcrule_social_protection`) are masked through the same task mask: their `data`, the string form of the
  beneficiary's bill, keeps that form with the bill's `terms` and the `code` of its lines (the person's names, or the
  group's code) shown as the mask, for a beneficiary while a name column of `individual`'s person mask is masked, for a
  group beneficiary while `individual`'s group mask masks `code`. While either is masked, data that cannot be read as
  that bill is masked whole.
* `BeneficiaryGQLType` and `GroupBeneficiaryGQLType` apply the mask on every path that reaches them (`beneficiary`,
  `groupBeneficiary`, `BenefitPlan.beneficiarySet` and `.groupbeneficiarySet`, `Individual.beneficiarySet`,
  `Group.groupbeneficiarySet`, any other nested path), `jsonExt` included.
* A filter reading a masked field is refused, naming the lookup, on every connection of the two types: the person and
  group filters, `search` (person names, `json_ext`, group code and head names), and `customFilters` (on the
  beneficiary's `json_ext`, or the members' `json_ext` for group beneficiaries).
* Every ordered connection of the module (`benefitPlan`, `benefitPlanHistory`, `beneficiaryDataUploadHistory`,
  `activity`, `project`, `projectHistory`, `beneficiary`, `groupBeneficiary`) is `individual`'s
  `MaskedOrderedConnectionField`: an `orderBy` term whose path, through any relation, reads a masked field (a masked
  `json_ext` key of a beneficiary, person or group included) or traverses a JSON field no mask describes (the
  `json_ext` of a plan, project or activity, for instance) is left out and logged at info level, and the primary key
  orders last. An unmasked `json_ext` key of a beneficiary still orders. `headFirstName`, `headLastName` and `headDob` are checked as the head's fields they read;
  `isEligible`, the eligibility result each row shows, orders for every user.
* `beneficiaryExport` and `groupBeneficiaryExport` refuse the same filters and write the columns reading a masked field
  masked (a whole `jsonExt` column with its masked keys masked), as `individual`'s exports do; the head columns
  (`headFirstName`, `headLastName`, `headDob`) are masked as the person fields they read.
* `validate_import_beneficiaries` validates only the rows of the upload the user may see (`IndividualDataSource`
  row scope: the uploader's and reviewers' for pending rows) and returns them masked by `individual`'s upload row mask,
  with the validation result of each masked key masked but its success.
* `download_invalid_items` masks each row by `individual`'s upload row mask; `download_beneficiary_upload_file`, which
  returns the stored file, is refused while that mask masks any field.


## openIMIS Modules Dependencies
- core
- individual

## OpenSearch

### Available Documents 
* BeneficiaryDocument

### How to initlaize data after deployment
* If you have initialized the application but still have some data to be transferred, you can effortlessly 
achieve this by using the commands available in this module: `python manage.py add_beneficiary_data_to_opensearch`. 
This command loads existing data into OpenSearch.

### How to Import a Dashboard
* Locate the dashboard definition file in `.ndjson` format within 
the `openimis-be_social_protection/import_data` directory.
* Log in to your OpenSearch instance.
* Expand the sidebar located on the left side of the page.
* Navigate to `Management` and select `Dashboards Management`.
* On the left side of the page, click on `Saved Objects`.
* At the top-right corner of the table, click on `Import`.
* A new side-modal will appear on the right side of the page. 
Drag and drop the file from `openimis-be_social_protection/import_data` into the import dropzone.
* This action will import the dashboards along with related 
charts that should be accessible on the visualization page.
* Verify if the dashboards have been imported properly.

### File for importing in .ndsjon format
* This file contains dashboard definitions that can be easily uploaded, as described in the "How to Import a Dashboard" 
section above. It includes definitions of dashboards and the visualizations contained within them.

### How to Export Dashboards with Related Objects like Visualizations in OpenSearch?
* Log in to your OpenSearch instance.
* Expand the sidebar located on the left side of the page.
* Navigate to `Management` and select `Dashboards Management`.
* On the left side of the page, click on `Saved Objects`.
* At the top-right corner of the table, click on `Export <N> objects`.
* Ensure that you have selected dashboards only. Additionally, choose the option to 
include related objects, and then click export all.
* You should have downloaded file in `.ndjson` format. 
* Save file in the business model for initialization after deployment in 
`openimis-be_social_protection/import_data`.
* Rename filename into `opensearch_beneficiary_dashboard.ndjson`

## Validations and deduplication detection

### Validations endpoint
* This is handled by the POST endpoint 'api/social_protection/validate_import_beneficiaries'.
* The endpoint is utilized within the upload workflow when a user uploads beneficiaries into a specific system.
* The input required is identical to that of the POST endpoint 'api/social_protection/import_beneficiaries' (CSV file). 
* The endpoint heavily relies on schema properties. For instance, `validationCalculation` in the schema triggers a specific validation strategy. Similarly, in the duplications section of the schema, 
setting `uniqueness: true` signifies the need for duplication checks based on the record's field value.
* Based on the provided schema below (from `programme/benefit plan`), it indicates that validations will run for the `email` 
field (`validationCalculation`), and duplication checks will be performed for `national_id` (`uniqueness: true`)
```
{
   "$id":"https://example.com/beneficiares.schema.json",
   "type":"object",
   "title":"Record of beneficiares",
   "$schema":"http://json-schema.org/draft-04/schema#",
   "properties":{
      "email":{
         "type":"string",
         "description":"email address to contact with beneficiary",
         "validationCalculation":{
            "name":"EmailValidationStrategy"
         }
      },
      "able_bodied":{
         "type":"boolean",
         "description":"Flag determining whether someone is able bodied or not"
      },
      "national_id":{
         "type":"string",
         "uniqueness":true,
         "description":"national id"
      },
      "educated_level":{
         "type":"string",
         "description":"The level of person when it comes to the school/education/studies"
      },
      "chronic_illness":{
         "type":"boolean",
         "description":"Flag determining whether someone has such kind of illness or not"
      },
      "national_id_type":{
         "type":"string",
         "description":"A type of national id"
      },
      "number_of_elderly":{
         "type":"integer",
         "description":"Number of elderly"
      },
      "number_of_children":{
         "type":"integer",
         "description":"Number of children"
      },
      "beneficiary_data_source":{
         "type":"string",
         "description":"The source from where such beneficiary comes"
      }
   },
   "description":"This document records the details beneficiares"
}
```
* An example response after calling the endpoint looks like this:
```
{
   "success":true,
   "data":[
      {
         "row":{
            "first_name":"Rick",
            "last_name":"Scott",
            "dob":"2023-07-13",
            "email":"testtesttest@test.com",
            "able_bodied":false,
            "national_id":"1345320000AN",
            "educated_level":"higher education",
            "national_id_type":"National ID Card",
            "number_of_elderly":1,
            "number_of_children":2,
            "beneficiary_data_source":"BENEFICIARY_ETL"
         },
         "validations":{
            "email":{
               "success":true,
               "field_name":"email",
               "note":"Ok",
               "duplications":null
            },
            "national_id_uniqueness":{
               "success":false,
               "field_name":"national_id",
               "note":"'national_id' Field value '1345320000AN' is duplicated",
               "duplications":{
                  "duplicated":true,
                  "duplicates_amoung_database":[
                     {
                        "id":"dbe11b3d-c6db-4912-bc84-c8e3d57afdb7",
                        "first_name":"TestFN",
                        "last_name":"TestLN",
                        "dob":"2023-07-13",
                        "email":"testtesttest@test.com",
                        "able_bodied":false,
                        "national_id":"1345320000AN",
                        "educated_level":"higher education",
                        "national_id_type":"National ID Card",
                        "number_of_elderly":1,
                        "number_of_children":2,
                        "beneficiary_data_source":"BENEFICIARY_ETL"
                     },
                     {
                        "id":"8321950f-a017-4940-a7ac-977714b685ec",
                        "first_name":"Lewis",
                        "last_name":"Test",
                        "dob":"1998-06-04",
                        "able_bodied":true,
                        "national_id":"1345320000AN",
                        "educated_level":"higher education",
                        "national_id_type":"passport",
                        "number_of_elderly":0,
                        "number_of_children":0,
                        "beneficiary_data_source":"BENEFICIARY_ETL"
                     },
                     {
                        "id":"bc0c2772-fcfa-46a4-9b42-894707db2c37",
                        "first_name":"Jacob",
                        "last_name":"Open",
                        "dob":"1995-06-01",
                        "able_bodied":false,
                        "national_id":"1345320000AN",
                        "educated_level":"higher education",
                        "national_id_type":"passport",
                        "number_of_elderly":0,
                        "number_of_children":2,
                        "beneficiary_data_source":"BENEFICIARY_ETL"
                     },
                     {
                        "id":"ea21f84c-28db-4039-96d4-460a96bb2278",
                        "first_name":"Jacob",
                        "last_name":"Open",
                        "dob":"1995-06-01",
                        "able_bodied":true,
                        "national_id":"1345320000AN",
                        "educated_level":"higher education",
                        "national_id_type":"passport",
                        "number_of_elderly":0,
                        "number_of_children":4,
                        "beneficiary_data_source":"BENEFICIARY_ETL"
                     }
                  ],
                  "incoming_duplicates":[
                     {
                        "first_name":"Eva",
                        "last_name":"Jacob",
                        "dob":"1995-06-01",
                        "email":"22121211221dsdsdsds2@gmail.com",
                        "able_bodied":true,
                        "national_id":"1345320000AN",
                        "educated_level":"secondary education",
                        "national_id_type":"National ID Card",
                        "number_of_elderly":1,
                        "number_of_children":2,
                        "beneficiary_data_source":"BENEFICIARY_ETL"
                     }
                  ]
               }
            }
         }
      },
      {
         "row":{
            "first_name":"Frank",
            "last_name":"Mood",
            "dob":"1995-06-01",
            "email":"frank.mood@test.com",
            "able_bodied":true,
            "national_id":"134532022LKSD",
            "educated_level":"medium education",
            "national_id_type":"National ID Card",
            "number_of_elderly":0,
            "number_of_children":1,
            "beneficiary_data_source":"BENEFICIARY_ETL"
         },
         "validations":{
            "email":{
               "success":true,
               "field_name":"email",
               "note":"Ok",
               "duplications":null
            },
            "national_id_uniqueness":{
               "success":true,
               "field_name":"national_id",
               "note":"'national_id' Field value '134532022LKSD' is not duplicated",
               "duplications":null
            }
         }
      },
      {
         "row":{
            "first_name":"Jan",
            "last_name":"White",
            "dob":"1995-06-01",
            "email":"janwhitetest.com",
            "able_bodied":true,
            "national_id":"1345320000ANER",
            "educated_level":"higher education",
            "national_id_type":"National ID Card",
            "number_of_elderly":0,
            "number_of_children":4,
            "beneficiary_data_source":"BENEFICIARY_ETL"
         },
         "validations":{
            "email":{
               "success":false,
               "field_name":"email",
               "note":"Invalid email format",
               "duplications":null
            },
            "national_id_uniqueness":{
               "success":true,
               "field_name":"national_id",
               "note":"'national_id' Field value '1345320000ANER' is not duplicated",
               "duplications":null
            }
         }
      },
   ]
}
```
* Within the example response, the `data` section contains information about each row in an array format.
* Each element in the array is a dictionary representing a row from the input CSV file. 
* Inside this dictionary, the `row` key holds the representation of a new individual/beneficiary entering the system with provided values.
* Under `validations`, you'll find validated fields (if the field in the schema is marked by a validation class) and potential duplicates if 
`uniqueness` property is set for the field.  
If the property `uniqueness` is set for a particular field, in `validations`, an additional key suffix `_uniqueness` indicates potential duplicates. 
* The 'duplication' section shows potential duplicates among incoming (`incoming_duplicates`) and existing records (`duplicates_amoung_database`).
* An empty `validation` property indicates that no validations need processing based on the schema properties. 
* Next to the `data` and `success` properties, there is a `summary_invalid_items` field containing a list of uuids of individual data sources which are invalid. 
This list is necessary in the Benefit Update workflow to flag such records in the IndividualDataSource.

### Validations in upload workflow
* https://github.com/openimis/openimis-lightning_dkr/tree/develop Here there are two workflows responsible for uploading and validation data: `BenefitPlanUpdate` and `beneficiary-import-valid-items`
* The workflow `BenefitPlanUpdate` is utilized when a file is uploaded using a form in BenefitPlanPage.
* The workflow named `beneficiary-import-valid-items` is activated to confirm valid items following the validation process. Its activation occurs when a task linked to that specific action is initiated.
* The validation operates according to the calculation rule, defining the strategy for determining validation approaches.
* More about calculation strategy verification in calcrule strategy you can find more in [README Section in calculation validation strategy module](https://github.com/openimis/openimis-be-calcrule_validations_py/tree/develop) 
* The upload process involves two stages: first, a validation process verifies the data, and upon successful validation, 
the data is uploaded. In case of any invalid items, there's an additional step where the user can review and download a 
report containing the invalid items. After reviewing the report, the user can proceed to import the valid items through task management.
* For successful scenarios, the status is marked as `SUCCESS`. There's no requirement for maker-checker validation (task) since the process wasn't halted.
* In scenarios where one or more records are invalid, the status is `WAITING_FOR_VERIFICATION`. This indicates the presence of a task in the maker-checker view for verifying the upload of valid items. 
The report containing invalid items can be downloaded from the upload history on the benefit plan page.
* When a user accepts the valid items from an import that faced issues with some invalid items and there are no errors in this workflow, 
the status of the import is marked as `PARTIAL_SUCCESS`. This triggers the `beneficiary-import-valid-items` workflow in such cases. 


### Enabling Python Workflows
Module comes with simple workflows for data upload. 
They should be used for the development purposes, not in production environment. 
To activate these Python workflows, a configuration change is required. 
Specifically, the `enable_python_workflows` parameter to `true` within module config.

Workflows: 
 * beneficiary upload