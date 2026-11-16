"""Bounded JSON Schema subset compiled into strict Pydantic output models."""

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    create_model,
    model_validator,
)

JsonScalar = StrictStr | StrictInt | StrictFloat | StrictBool
JsonType = Literal["object", "string", "integer", "number", "boolean", "array", "null"]


class OutputSchema(BaseModel):
    """A limited, explicit schema accepted from call API clients."""

    model_config = ConfigDict(extra="forbid")

    # JSON Schema permits a list of types to express nullable values, e.g.
    # {"type": ["integer", "null"]}. Keep this subset bounded to known JSON types.
    type: JsonType | list[JsonType]
    description: str | None = Field(default=None, max_length=500)
    properties: dict[str, "OutputSchema"] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    items: "OutputSchema | None" = None
    enum: list[JsonScalar] | None = None
    additionalProperties: Literal[False] = False

    @model_validator(mode="after")
    def validate_schema_shape(self) -> "OutputSchema":
        types = [self.type] if isinstance(self.type, str) else self.type
        if not types or len(set(types)) != len(types):
            raise ValueError("type must contain one or more unique JSON Schema types")
        if len(types) > 2 or (len(types) == 2 and "null" not in types):
            raise ValueError("type unions may only combine one JSON type with null")
        non_null_types = [value for value in types if value != "null"]
        if len(non_null_types) > 1:
            raise ValueError("type unions may only combine one JSON type with null")
        effective_type = non_null_types[0] if non_null_types else "null"

        if effective_type == "object":
            if not self.properties:
                raise ValueError("object schemas must define at least one property")
            if len(self.properties) > 50:
                raise ValueError("object schemas may define at most 50 properties")
            if len(set(self.required)) != len(self.required):
                raise ValueError("required property names must be unique")
            if not set(self.required).issubset(self.properties):
                raise ValueError("required must contain only defined properties")
            if self.items is not None or self.enum is not None:
                raise ValueError("object schemas cannot define items or enum")
        elif effective_type == "array":
            if self.items is None:
                raise ValueError("array schemas must define items")
            if self.properties or self.required or self.enum is not None:
                raise ValueError("array schemas cannot define properties, required, or enum")
        elif self.properties or self.required or self.items is not None:
            raise ValueError("scalar schemas cannot define properties, required, or items")
        if self.enum is not None:
            if not self.enum or len(self.enum) > 100:
                raise ValueError("enum must contain between 1 and 100 values")
            if len({(type(value).__name__, repr(value)) for value in self.enum}) != len(self.enum):
                raise ValueError("enum values must be unique")
            expected = {
                "string": str,
                "integer": int,
                "number": (int, float),
                "boolean": bool,
            }.get(effective_type)
            expected_types = (expected,) if isinstance(expected, type) else expected
            if expected_types is None or any(type(value) not in expected_types for value in self.enum):
                raise ValueError("enum values must match a scalar schema type")
        return self


OutputSchema.model_rebuild()


def validate_output_schema(value: dict[str, Any]) -> OutputSchema:
    schema = OutputSchema.model_validate(value)
    if schema.type != "object":
        raise ValueError("the post-call output schema root must be an object")
    _check_depth(schema, 0)
    return schema


def _check_depth(schema: OutputSchema, depth: int) -> None:
    if depth > 5:
        raise ValueError("output schemas may be nested at most 5 levels")
    for child in schema.properties.values():
        _check_depth(child, depth + 1)
    if schema.items is not None:
        _check_depth(schema.items, depth + 1)


def build_output_model(schema: OutputSchema, *, name: str = "CallOutput") -> type[BaseModel]:
    """Compile the accepted schema into a strict Pydantic model."""

    def annotation_for(node: OutputSchema, field_index: int) -> Any:
        types = [node.type] if isinstance(node.type, str) else node.type
        nullable = "null" in types
        effective_type = next((value for value in types if value != "null"), "null")
        if effective_type == "object":
            annotation = build_output_model(node, name=f"{name}_Object{field_index}")
        elif effective_type == "array":
            assert node.items is not None
            annotation = list[annotation_for(node.items, field_index)]
        elif effective_type == "null":
            annotation = type(None)
        else:
            types_by_name = {
                "string": StrictStr,
                "integer": StrictInt,
                "number": StrictFloat,
                "boolean": StrictBool,
            }
            base_type = types_by_name[effective_type]
            if node.enum is None:
                annotation = base_type
            else:
                enum_type = Enum(
                    f"{name}_Enum{field_index}",
                    {f"value_{index}": value for index, value in enumerate(node.enum)},
                )
                annotation = enum_type
        if nullable:
            return annotation | type(None)
        return annotation

    fields: dict[str, tuple[Any, Any]] = {}
    for field_index, (field_name, node) in enumerate(schema.properties.items()):
        annotation = annotation_for(node, field_index)
        default: Any = ... if field_name in schema.required else None
        alias_field_name = f"field_{field_index}"
        fields[alias_field_name] = (
            Annotated[annotation, Field(alias=field_name, description=node.description)]
            if node.description
            else Annotated[annotation, Field(alias=field_name)],
            default,
        )
    return create_model(
        name,
        __config__=ConfigDict(extra="forbid", use_enum_values=True),
        **fields,
    )
