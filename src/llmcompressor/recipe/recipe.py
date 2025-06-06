import json
import os
import re
from typing import Any, Dict, List, Optional, Union

import yaml
from loguru import logger
from pydantic import Field, model_validator

from llmcompressor.modifiers import Modifier, StageModifiers
from llmcompressor.recipe.base import RecipeBase
from llmcompressor.recipe.modifier import RecipeModifier
from llmcompressor.recipe.stage import RecipeStage

__all__ = [
    "Recipe",
    "RecipeInput",
    "RecipeStageInput",
    "RecipeArgsInput",
]


class Recipe(RecipeBase):
    """
    A class to represent a recipe for a model.
    Recipes encode the instructions needed for modifying
    the model and/or training process as a list of modifiers.

    Recipes can be created from a file, string, or HuggingFace stub.
    Acceptable file formats include both json and yaml, however,
    when serializing a recipe, yaml will be used by default.
    """

    @classmethod
    def from_modifiers(
        cls,
        modifiers: Union[Modifier, List[Modifier]],
        modifier_group_name: Optional[str] = None,
    ) -> "Recipe":
        """
        Create a recipe instance from a list of modifiers

        (Note: all modifiers are wrapped into a single stage
        with the modifier_group_name as the stage name. If modifier_group_name is None,
        the default run type is `oneshot`)

        Lfecycle:
        | - Validate Modifiers
        | - Create recipe string from modifiers
        | - Create recipe instance from recipe string

        :param modifiers: The list of RecipeModifier instances
        :param modifier_group_name: The stage_name of the recipe,
            if `oneshot` or `train` the run_type of the recipe will be
            inferred from the modifier_group_name, if None, a dummy default
            group_name will be assigned.
        :return: The Recipe instance created from the modifiers
        """
        logger.info("Creating recipe from modifiers")

        if isinstance(modifiers, Modifier):
            modifiers = [modifiers]

        if any(not isinstance(modifier, Modifier) for modifier in modifiers):
            raise ValueError("modifiers must be a list of Modifier instances")

        group_name = modifier_group_name or "default"

        recipe_modifiers: List[RecipeModifier] = [
            RecipeModifier(
                type=modifier.__class__.__name__,
                group=group_name,
                args=modifier.model_dump(exclude_unset=True),
            )
            for modifier in modifiers
        ]
        # assume one stage for modifier instances
        stages: List[RecipeStage] = [
            RecipeStage(group=group_name, modifiers=recipe_modifiers)
        ]
        recipe = cls()
        recipe.stages = stages
        return recipe

    @classmethod
    def create_instance(
        cls,
        path_or_modifiers: Union[str, Modifier, List[Modifier], "Recipe"],
        modifier_group_name: Optional[str] = None,
    ) -> "Recipe":
        """
        Create a recipe instance from a file, string, or RecipeModifier objects


        Using a recipe string or file is supported:
        >>> recipe_str = '''
        ... test_stage:
        ...     pruning_modifiers:
        ...         ConstantPruningModifier:
        ...             start: 0.0
        ...             end: 2.0
        ...             targets: ['re:.*weight']
        ... '''
        >>> recipe = Recipe.create_instance(recipe_str)

        :param path_or_modifiers: The path to the recipe file or
            or the recipe string (must be a valid
            json/yaml file or a valid json/yaml string). Can also
            accept a RecipeModifier instance, or a list of
            RecipeModifiers
        :param modifier_group_name: The stage_name of the recipe,
            if `oneshot` or `train` the run_type of the recipe will be
            inferred from the modifier_group_name, if None, a dummy default
            group_name will be assigned. This argument is only used
            when creating a recipe from a Modifier/list of Modifier(s)
            instance, else it's ignored.
        :return: The Recipe instance created from the path or modifiers,
            or a valid recipe string in yaml/json format
        """
        if isinstance(path_or_modifiers, Recipe):
            # already a recipe
            return path_or_modifiers

        if isinstance(path_or_modifiers, (Modifier, list)):
            return cls.from_modifiers(
                modifiers=path_or_modifiers, modifier_group_name=modifier_group_name
            )

        if not os.path.isfile(path_or_modifiers):
            # not a local file
            # assume it's a string
            logger.debug(
                "Could not initialize recipe as a file path or zoo stub, "
                "attempting to process as a string."
            )
            logger.debug(f"Input string: {path_or_modifiers}")
            obj = _load_json_or_yaml_string(path_or_modifiers)
            return Recipe.model_validate(obj)
        else:
            logger.info(f"Loading recipe from file {path_or_modifiers}")

        with open(path_or_modifiers, "r") as file:
            content = file.read().strip()
            if path_or_modifiers.lower().endswith(".md"):
                content = _parse_recipe_from_md(path_or_modifiers, content)

            if path_or_modifiers.lower().endswith(".json"):
                obj = json.loads(content)
            elif path_or_modifiers.lower().endswith(
                ".yaml"
            ) or path_or_modifiers.lower().endswith(".yml"):
                obj = yaml.safe_load(content)
            else:
                try:
                    obj = _load_json_or_yaml_string(content)
                except ValueError:
                    raise ValueError(
                        f"Could not parse recipe from path {path_or_modifiers}"
                    )
            return Recipe.model_validate(obj)

    @staticmethod
    def simplify_recipe(
        recipe: Optional["RecipeInput"] = None,
        target_stage: Optional["RecipeStageInput"] = None,
        override_args: Optional["RecipeArgsInput"] = None,
    ) -> "Recipe":
        """
        Simplify a Recipe by removing stages that are not in the target_stages
        and updating args if overrides are provided

        :param recipe: The Recipe instance to simplify
        :param target_stages: The stages to target when simplifying the recipe
        :param override_args: The arguments used to override existing recipe args
        :return: The simplified Recipe instance
        """
        if recipe is None or (isinstance(recipe, list) and len(recipe) == 0):
            return Recipe()

        # prepare recipe
        if (
            isinstance(recipe, Modifier)
            or isinstance(recipe, str)
            or (
                isinstance(recipe, list)
                and all(isinstance(mod, Modifier) for mod in recipe)
            )
        ):
            recipe = Recipe.create_instance(recipe)
        # Filter stages if target_stages are provided
        if target_stage:
            recipe.stages = [
                stage for stage in recipe.stages if (stage.group in target_stage)
            ]
        # Apply argument overrides if provided
        if override_args:
            recipe.args = {**recipe.args, **override_args}
        return recipe

    @staticmethod
    def simplify_combine_recipes(
        recipes: List[Union[str, "Recipe"]],
    ) -> "Recipe":
        """
        A method to combine multiple recipes into one recipe
        Automatically calculates the start and end of the combined recipe
        and shifts the start and end of the recipes accordingly

        :param recipes: The list of Recipe instances to combine
        :return: The combined Recipe instance
        """

        combined = Recipe()
        for recipe in recipes:
            simplified = Recipe.simplify_recipe(
                recipe=recipe,
            )
            combined.version = simplified.version
            combined.stages.extend(simplified.stages)
            combined.args.update(simplified.args)

        return combined

    version: str = None
    args: Dict[str, Any] = Field(default_factory=dict)
    stages: List[RecipeStage] = Field(default_factory=list)

    def create_modifier(self) -> List["StageModifiers"]:
        """
        Create and return a list of StageModifiers for each stage in the recipe

        >>> recipe_str = '''
        ... test_stage:
        ...     pruning_modifiers:
        ...         ConstantPruningModifier:
        ...             start: 0.0
        ...             end: 2.0
        ...             targets: ['re:.*weight']
        ... '''
        >>> recipe = Recipe.create_instance(recipe_str)
        >>> stage_modifiers = recipe.create_modifier()
        >>> len(stage_modifiers) == 1
        True
        >>> len(stage_modifiers[0].modifiers) == 1
        True

        :return: A list of StageModifiers for each stage in the recipe
        """
        modifiers = []

        for index, stage in enumerate(self.stages):
            stage_modifiers = stage.create_modifier()
            stage_modifiers.index = index
            stage_modifiers.group = stage.group
            modifiers.append(stage_modifiers)

        return modifiers

    @model_validator(mode="before")
    @classmethod
    def remap_stages(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        stages = []

        modifiers = RecipeStage.extract_dict_modifiers(values)
        if modifiers:
            default_stage = {"modifiers": modifiers, "group": "default"}
            stages.append(default_stage)

        extracted = Recipe.extract_dict_stages(values)
        stages.extend(extracted)
        formatted_values = {}

        # fill out stages
        formatted_values["stages"] = stages

        # fill out any default argument values
        args = {}
        for key, val in values.items():
            args[key] = val
        formatted_values["args"] = args

        return formatted_values

    @staticmethod
    def extract_dict_stages(values: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract stages from a dict of values, acceptable dictionary structures
        are shown below

        Accepted stage formats:
        - stages:
          first_stage:
            modifiers: ...
          second_stage:
            modifiers: ...

        - first_stage:
          modifiers: ...
        - second_stage:
          modifiers: ...

        Accepted modifier formats default stage:
        - modifiers:
          - ModifierTypeOne
            ...
          - ModifierTypeTwo
            ...

        - first_modifiers:
          - ModifierTypeOne
            ...
          - ModifierTypeTwo
            ...

        >>> values = {
        ... "stages": {
        ...     "first_stage": {
        ...         "modifiers": {
        ...             "ModifierTypeOne": {
        ...                 "start": 0.0,
        ...                 "end": 2.0,
        ...                 }
        ...         }
        ...     }
        ... }
        ... }
        >>> Recipe.extract_dict_stages(values) # doctest: +NORMALIZE_WHITESPACE
        [{'modifiers': {'ModifierTypeOne': {'start': 0.0, 'end': 2.0}},
        'group': 'first_stage'}]

        :param values: The values dict to extract stages from
        :return: A list of stages, where each stage is a dict of
            modifiers and their group
        """

        stages = []
        remove_keys = []

        default_modifiers = RecipeStage.extract_dict_modifiers(values)
        if default_modifiers:
            default_stage = {"modifiers": default_modifiers, "group": "default"}
            stages.append(default_stage)

        if "stages" in values and values["stages"]:
            assert isinstance(
                values["stages"], dict
            ), f"stages must be a dict, given {values['stages']}"
            remove_keys.append("stages")

            for key, value in values["stages"].items():
                assert isinstance(value, dict), f"stage must be a dict, given {value}"
                value["group"] = key
                stages.append(value)

        for key, value in list(values.items()):
            if key.endswith("_stage"):
                remove_keys.append(key)
                value["group"] = key.rsplit("_stage", 1)[0]
                stages.append(value)

        for key in remove_keys:
            del values[key]

        return stages

    def dict(self, *args, **kwargs) -> Dict[str, Any]:
        """
        :return: A dictionary representation of the recipe
        """
        dict_ = super().model_dump(*args, **kwargs)
        stages = {}

        for stage in dict_["stages"]:
            name = f"{stage['group']}_stage"
            del stage["group"]

            if name not in stages:
                stages[name] = []

            stages[name].append(stage)

        dict_["stages"] = stages

        return dict_

    def yaml(self, file_path: Optional[str] = None) -> str:
        """
        Return a yaml string representation of the recipe.

        :param file_path: optional file path to save yaml to
        :return: The yaml string representation of the recipe
        """
        file_stream = None if file_path is None else open(file_path, "w")
        yaml_dict = self._get_yaml_dict()

        ret = yaml.dump(
            yaml_dict,
            stream=file_stream,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=None,
            width=88,
        )

        if file_stream is not None:
            file_stream.close()

        return ret

    def _get_yaml_dict(self) -> Dict[str, Any]:
        """
        Get a dictionary representation of the recipe for yaml serialization
        The returned dict will only contain information necessary for yaml
        serialization and must not be used in place of the dict method

        :return: A dictionary representation of the recipe for yaml serialization
        """

        original_recipe_dict = self.dict()
        yaml_recipe_dict = {}

        # populate recipe level attributes
        recipe_level_attributes = ["version", "args"]

        for attribute in recipe_level_attributes:
            if attribute_value := original_recipe_dict.get(attribute):
                yaml_recipe_dict[attribute] = attribute_value

        # populate stages
        stages = original_recipe_dict["stages"]
        for stage_name, stage_list in stages.items():
            for idx, stage in enumerate(stage_list):
                if len(stage_list) > 1:
                    # resolve name clashes caused by combining recipes with
                    # duplicate stage names
                    final_stage_name = f"{stage_name}_{idx}"
                else:
                    final_stage_name = stage_name
                stage_dict = get_yaml_serializable_stage_dict(
                    modifiers=stage["modifiers"]
                )

                # infer run_type from stage
                if run_type := stage.get("run_type"):
                    stage_dict["run_type"] = run_type

                yaml_recipe_dict[final_stage_name] = stage_dict

        return yaml_recipe_dict


RecipeInput = Union[str, List[str], Recipe, List[Recipe], Modifier, List[Modifier]]
RecipeStageInput = Union[str, List[str], List[List[str]]]
RecipeArgsInput = Union[Dict[str, Any], List[Dict[str, Any]]]


def _load_json_or_yaml_string(content: str) -> Dict[str, Any]:
    # try loading as json first, then yaml
    # if both fail, raise a ValueError
    try:
        ret = json.loads(content)
    except json.JSONDecodeError:
        try:
            ret = yaml.safe_load(content)
        except yaml.YAMLError as err:
            raise ValueError(f"Could not parse recipe from string {content}") from err

    if not isinstance(ret, dict):
        raise ValueError(
            f"Could not parse recipe from string {content}. If you meant load from "
            "a file, please make sure that the specified file path exists"
        )
    return ret


def _parse_recipe_from_md(file_path, yaml_str):
    """
    extract YAML front matter from markdown recipe card. Copied from
    llmcompressor.optim.helpers:_load_yaml_str_from_file
    :param file_path: path to recipe file
    :param yaml_str: string read from file_path
    :return: parsed yaml_str with README info removed
    """
    # extract YAML front matter from markdown recipe card
    # adapted from
    # https://github.com/jonbeebe/frontmatter/blob/master/frontmatter
    yaml_delim = r"(?:---|\+\+\+)"
    yaml = r"(.*?)"
    re_pattern = r"^\s*" + yaml_delim + yaml + yaml_delim
    regex = re.compile(re_pattern, re.S | re.M)
    result = regex.search(yaml_str)

    if result:
        yaml_str = result.group(1)
    else:
        # fail if we know whe should have extracted front matter out
        raise RuntimeError(
            "Could not extract YAML front matter from recipe card:" " {}".format(
                file_path
            )
        )
    return yaml_str


def get_yaml_serializable_stage_dict(modifiers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    This function is used to convert a list of modifiers into a dictionary
    where the keys are the group names and the values are the modifiers
    which in turn are dictionaries with the modifier type as the key and
    the modifier args as the value.
    This is needed to conform to our recipe structure during yaml serialization
    where each stage, modifier_groups, and modifiers are represented as
    valid yaml dictionaries.

    Note: This function assumes that modifier groups do not contain the same
    modifier type more than once in a group. This assumption is also held by
    Recipe.create_instance(...) method.

    :param modifiers: A list of dictionaries where each dictionary
        holds all information about a modifier
    :return: A dictionary where the keys are the group names and the values
        are the modifiers which in turn are dictionaries with the modifier
        type as the key and the modifier args as the value.
    """
    stage_dict = {}
    for modifier in modifiers:
        group_name = f"{modifier['group']}_modifiers"
        modifier_type = modifier["type"]
        if group_name not in stage_dict:
            stage_dict[group_name] = {}
        stage_dict[group_name][modifier_type] = modifier["args"]
    return stage_dict
