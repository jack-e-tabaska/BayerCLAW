from collections import Counter
from itertools import chain

from voluptuous import *

from .util import Step


class CompilerError(Exception):
    def __init__(self, invalid_exception: Invalid, where=None):
        super().__init__(invalid_exception.msg)
        self.where = where
        self.field = ".".join(str(p) for p in invalid_exception.path)
        self.message = invalid_exception.error_message

    def __str__(self) -> str:
        if self.where is not None:
            if self.field:
                ret = f"in {self.where}, field '{self.field}': {self.message}"
            else:
                ret = f"in {self.where}: {self.message}"
        else:
            if self.field:
                ret = f"field '{self.field}': {self.message}"
            else:
                ret = self.message
        return ret


def no_shared_keys(*field_names):
    def _impl(record: dict) -> dict:
        key_iters = ((record.get(f) or {}).keys() for f in field_names)
        keys = chain(*key_iters)
        key_counts = Counter(keys)
        dupes = [k for k, ct in key_counts.items() if ct > 1]
        if dupes:
            raise Invalid(f"duplicated keys: {', '.join(sorted(dupes))}")
        return record
    return _impl


skip_msg = "only one of 'skip_on_rerun' or 'skip_if_output_exists' is allowed"
next_or_end_msg = "cannot specify both 'next' and 'end' in a step"

next_or_end = {
    Exclusive("Next", "next_or_end", msg=next_or_end_msg): str,
    Exclusive("End", "next_or_end", msg=next_or_end_msg): All(Boolean(), Msg(True, "'end' value must be truthy")),
}

batch_step_schema = Schema(All(
    {
        Required("image"): str,
        Optional("task_role", default=None): Maybe(str),
        Optional("params", default={}): {str: Coerce(str)},
        # None is used as a signal that inputs was not specified at all, and should be copied from previous outputs.
        # inputs = {} can be used to explicitly specify a step has no inputs at all, with no copy from previous output.
        Optional("inputs", default=None): Any(None, {str: str}),
        Optional("references", default={}): {str: Match(r"^s3://", msg="reference values must be s3 paths")},
        Required("commands", msg="commands list is required"):
            All(Length(min=1, msg="at least one command is required"), [str]),
        Optional("outputs", default={}): {str: str},
        Exclusive("skip_if_output_exists", "skip_behavior", msg=skip_msg): bool,
        Exclusive("skip_on_rerun", "skip_behavior", msg=skip_msg): bool,
        Optional("compute", default={}): {
            Optional("cpus", default=1): All(int, Range(min=1)),
            Optional("memory", default="1 Gb"): Any(float, int, str, msg="memory must be a number or string"),
            Optional("spot", default=True): bool,
            Optional("queue_name", default=None): Maybe(str)
        },
        Optional("qc_check", default=None): Any(None, {
            Optional("type", default="choice"): str,
            Required("qc_result_file"): str,
            Required("stop_early_if"): str,
            Optional("email_subject", default="qc failure alert!"): str,
            Optional("notification", default=[]): [str],
        }),
        Optional("retry", default={}): {
            Optional("attempts", default=3): int,
            Optional("interval", default="3s"): Match(r"^\d+\s?[smhdw]$",
                                                      msg="incorrect retry interval time string"),
            Optional("backoff_rate", default=1.5): All(Any(float, int),
                                                       Clamp(min=1.0, msg="backoff rate must be at least 1.0")),
        },
        Optional("timeout", default=None): Any(None, Match(r"^\d+\s?[smhdw]$",
                                                           msg="incorrect timeout time string")),
        **next_or_end,
    },
    no_shared_keys("params", "inputs", "outputs", "references"),
))


native_step_schema = Schema(
    {
        Required("Type"):
            All(
                NotIn(["Choice", "Map"], msg="Choice and Map Types not supported"),
                Any("Pass", "Task", "Wait", "Succeed", "Fail", "Parallel",
                    msg="Type must be Pass, Task, Wait, Succeed, Fail, or Parallel")
            ),
        Extra: object,
    }
)


parallel_step_schema = Schema(
    {
        Optional("inputs", default={}): {str: str},
        Required("branches", msg="branches not found"):
            All(
                Length(min=1, msg="at least one branch is required"),
                [
                    {
                        Optional("if"): str,
                        Required("steps", msg="steps list not found"):
                            All(
                                Length(min=1, msg="at least one step is required"),
                                [dict]
                            )
                    },
                ]
            ),
        **next_or_end,
    }
)


chooser_step_schema = Schema(
    {
        Optional("inputs", default={}): {str: str},
        Required("choices", msg="choices list not found"):
            All(Length(min=1, msg="at least one choice is required"),
                [
                    {
                        Required("if", msg="no 'if' condition found"): str,
                        Required("next", msg="no 'next' name found"): str,
                    },
                ]),
        Optional("next"): str,
    }
)


scatter_step_schema = Schema(All(
    {
        Required("scatter"): {str: str},
        Optional("params", default={}): {str: Coerce(str)},
        Optional("inputs", default=None): Any(None, {str: str}),
        Required("steps", "steps list is required"):
            All(Length(min=1, msg="at least one step is required"), [{str: dict}]),
        Optional("outputs", default={}): Any(None, {str: str}),
        **next_or_end,
    },
    # It's technically OK if scatter shares keys with these, because it's namespaced as ${scatter.foo}
    no_shared_keys("params", "inputs", "outputs")
))


subpipe_step_schema = Schema(
    {
        Optional("submit", default=[]): [str],
        Required("subpipe"): str,
        Optional("retrieve", default=[]): [str],
        **next_or_end,
    }
)


workflow_schema = Schema(
    {
        Required("params", "params block not found"): {
            Optional("workflow_name", default=""): str,
            Optional("job_name", default=""): str,
            Required("repository", msg="repository is required"): str,
            Optional("task_role", default=None): Maybe(str),
        },
        Required("steps", "steps list not found"): All(Length(min=1, msg="at least one step is required"),
                                                       [{Coerce(str): dict}]),
    }
)


def _validator(spec: dict, schema: Schema, where: str):
    try:
        ret = schema(spec)
        return ret
    except Invalid as inv:
        raise CompilerError(inv, where=where)


def validate_batch_step(step: Step) -> Step:
    normalized_spec = _validator(step.spec, batch_step_schema, f"batch job step '{step.name}'")
    return Step(step.name, normalized_spec, step.next)


def validate_native_step(step: Step) -> Step:
    normalized_spec = _validator(step.spec, native_step_schema, f"native step '{step.name}")
    return Step(step.name, normalized_spec, step.next)


def validate_parallel_step(step: Step) -> Step:
    normalized_spec = _validator(step.spec, parallel_step_schema, f"parallel step '{step.name}")
    return Step(step.name, normalized_spec, step.next)


def validate_scatter_step(step: Step) -> Step:
    normalized_spec = _validator(step.spec, scatter_step_schema, f"scatter/gather step '{step.name}'")
    return Step(step.name, normalized_spec, step.next)


def validate_subpipe_step(step: Step) -> Step:
    normalized_spec = _validator(step.spec, subpipe_step_schema, f"subpipe step '{step.name}'")
    return Step(step.name, normalized_spec, step.next)


def validate_chooser_step(step: Step) -> Step:
    normalized_spec = _validator(step.spec, chooser_step_schema, f"chooser step '{step.name}'")
    return Step(step.name, normalized_spec, step.next)
