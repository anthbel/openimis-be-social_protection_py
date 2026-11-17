"""Hooks another module registers to check every write of enrolments (``Beneficiary``, ``GroupBeneficiary``).

A hook is an object with three methods, each called inside the writer's transaction:

- ``before_service_write(model, obj_data, creating)``: by ``BeneficiaryService`` and ``GroupBeneficiaryService``,
  inside the transaction of their create or update, before the row is read or written. It may adjust ``obj_data``;
  a ValidationError it raises is returned as the service's error result.
- ``before_write(model, plan_ids)`` and ``after_write(model, plan_ids, state)``: around a write of enrolments of
  the benefit plans ``plan_ids`` (sorted ids) that does not go through the services (``enrolment_write``).
  ``before_write`` returns a state that ``after_write`` receives once the write is done; a ValidationError
  ``after_write`` raises rolls the write back and reaches the writer's caller.
"""
from contextlib import contextmanager

from django.db import transaction

_hooks = []


def register_enrolment_hook(hook):
    if hook not in _hooks:
        _hooks.append(hook)


def unregister_enrolment_hook(hook):
    if hook in _hooks:
        _hooks.remove(hook)


def registered_enrolment_hooks():
    return tuple(_hooks)


def before_service_write(model, obj_data, creating):
    for hook in tuple(_hooks):
        hook.before_service_write(model, obj_data, creating)


@contextmanager
def enrolment_write(model, plan_ids):
    """Runs the block, a write of ``model`` rows of the benefit plans ``plan_ids``, in a transaction (a savepoint
    inside the caller's) between the hooks' ``before_write`` and ``after_write``."""
    plan_ids = sorted({str(plan_id) for plan_id in plan_ids if plan_id})
    with transaction.atomic():
        states = [(hook, hook.before_write(model, plan_ids)) for hook in tuple(_hooks)]
        yield
        for hook, state in states:
            hook.after_write(model, plan_ids, state)
