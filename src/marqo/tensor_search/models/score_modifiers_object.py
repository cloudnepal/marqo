import json
from typing import List, Dict, Any, Optional

from pydantic import BaseModel, validator, ValidationError

from marqo.core.models.score_modifier import ScoreModifierType, ScoreModifier
from marqo.api.exceptions import InvalidArgError
from marqo import marqo_docs

class ScoreModifierValidationError(InvalidArgError):
    def __init__(self, modifier: Dict[str, Any], message: str, link: str = None):
        super().__init__(
            link=link,
            message=f"Error validating score_modifiers = `{modifier}`. Reason: \n{message} "
                    f"Please revise your score_modifiers based on the provided error."
                    f"\n Check {marqo_docs.search_api_score_modifiers_parameter()} for more info."
        )


class ScoreModifierOperator(BaseModel):
    field_name: str
    weight: float = 1

    class Config:
        extra: str = "forbid"
        allow_mutation = False

    @validator('field_name')
    def name_not_id(cls, v):
        if v == "_id":
            raise InvalidArgError("_id is not allowed as a field_name")
        return v


class ScoreModifierLists(BaseModel):
    multiply_score_by: Optional[List[ScoreModifierOperator]] = None
    add_to_score: Optional[List[ScoreModifierOperator]] = None

    class Config:
        extra: str = "forbid"

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            raise ScoreModifierValidationError(modifier=data, message=json.dumps(e.errors()))

    @validator('multiply_score_by', 'add_to_score', pre=True, always=True)
    def at_least_one_must_be_provided(cls, v, values, field):
        """Validates that at least one of 'multiply_score_by', 'add_to_score' is non-null."""
        if field.name == 'add_to_score' and v is None and values.get('multiply_score_by') is None:
            raise ScoreModifierValidationError(modifier=values,
                                               message="At least one of multiply_score_by or add_to_score must be provided")
        return v

    @validator('multiply_score_by', 'add_to_score', pre=False, always=True)
    def at_least_one_item(cls, v, values, field):
        """Validate that, if present, the fields must be non-empty."""
        if v is not None and len(v) < 1:
            raise ScoreModifierValidationError(modifier=values,
                                               message=f"At least one ScoreModifierOperator is required in {field.name}")
        return v

    def to_marqo_score_modifiers(self) -> List[ScoreModifier]:
        """
        Convert this ScoreModifier to a list of marqo.core.models.marqo_query.ScoreModifier.
        """
        mult = [ScoreModifier(
            field=x.field_name,
            weight=x.weight,
            type=ScoreModifierType.Multiply
        ) for x in self.multiply_score_by] if self.multiply_score_by is not None else []
        add = [ScoreModifier(
            field=x.field_name,
            weight=x.weight,
            type=ScoreModifierType.Add
        ) for x in self.add_to_score] if self.add_to_score is not None else []

        return mult + add
