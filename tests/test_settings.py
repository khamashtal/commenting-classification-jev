"""Configuration: what happens when `config.toml` is wrong.

A configuration file is edited by hand, and every failure here is one a person would
otherwise make silently. The rule the whole module enforces is that a bad value stops
the process — it never falls back to a default, because a default that quietly replaces
what somebody meant to write is indistinguishable from the code ignoring them.

`tests/test_settings.py::test_the_shipped_config_is_valid` is the one test in the suite
that reads the project's real `config.toml`. Everything else builds its own, so that
retuning a threshold cannot break an unrelated test.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from processing import settings as settings_module
from processing.config import ConfigError, load_config, parse_config
from processing.questions import BATTERY
from processing.settings import CONFIG_PATH, SettingsError, get_settings, load_settings

QUESTION_IDS = frozenset(BATTERY)

# A minimal, valid file. Tests copy it and break exactly one thing, so a failure names
# the thing that was broken rather than the first of several.
VALID = """
[viafoura]
section_uuid = "00000000-0000-4000-8000-010fdf3f0a45"
base_url = "https://livecomments.invalid/"
page_size = 100
reply_limit = 50
timeout_seconds = 30.0
max_concurrent_requests = 8
max_retries = 3
backoff_initial_seconds = 0.5
backoff_max_seconds = 30.0

[jev]
model = ""
concurrency = 64
timeout_seconds = 30.0
requests_per_minute = 1200
tokens_per_second = 250000

[article]
max_words = 600

[classification]
min_words = 15
max_caps_ratio = 0.5
sweet_spot = [20, 100]
on_topic_exclude_below = 0.35
on_topic_flag_below = 0.60

[classification.weights]
experience = 0.35
tone = 0.15
readability = 0.10
contribution = 0.15
standalone = 0.10
representativeness = 0.15

[classification.exclude_at]
sarcasm = 0.70

[classification.flag_at]
sarcasm = 0.40

[api]
host = "127.0.0.1"
port = 8000
cors_origins = ["http://localhost:3000"]
default_min_score = 0.50
max_results = 25
min_results = 5
lock_timeout_seconds = 30.0

[paths]
state_dir = "state"
feedback_dir = "feedback"
output_dir = "output"
"""


def write_config(directory: Path, body: str = VALID) -> Path:
    path = directory / "config.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def load(directory: Path, body: str = VALID):  # noqa: ANN201 - returns a Config
    return load_config(write_config(directory, body), known_question_ids=QUESTION_IDS)


class TestReadingTheFile:
    def test_a_valid_file_round_trips(self, tmp_path: Path) -> None:
        config = load(tmp_path)
        assert config.article.max_words == 600
        assert config.classification.sweet_spot == (20, 100)
        assert config.api.cors_origins == ("http://localhost:3000",)

    def test_an_empty_model_means_the_api_chooses(self, tmp_path: Path) -> None:
        """TOML has no null, so "" is how the file says "leave it to jev-latest"."""
        assert load(tmp_path).jev.model is None

    def test_a_pinned_model_survives(self, tmp_path: Path) -> None:
        config = load(tmp_path, VALID.replace('model = ""', 'model = "jev-1.13.0"'))
        assert config.jev.model == "jev-1.13.0"

    def test_paths_are_anchored_to_the_file_not_the_cwd(self, tmp_path: Path) -> None:
        """Run from anywhere, write to the same place."""
        config = load(tmp_path)
        assert config.paths.state_dir == tmp_path / "state"
        assert config.paths.state_dir.is_absolute()

    def test_an_absolute_path_is_left_alone(self, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        config = load(
            tmp_path,
            VALID.replace('state_dir = "state"', f'state_dir = "{elsewhere}"'),
        )
        assert config.paths.state_dir == elsewhere

    def test_a_trailing_slash_on_the_base_url_is_dropped(self, tmp_path: Path) -> None:
        """Otherwise every joined path gets a double slash."""
        assert not load(tmp_path).viafoura.base_url.endswith("/")

    def test_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        """Not an empty config, not defaults: the file is committed, so its absence is
        a broken checkout rather than an instruction to guess.
        """
        with pytest.raises(ConfigError, match="No configuration file"):
            load_config(
                tmp_path / "config.toml",
                known_question_ids=QUESTION_IDS,
            )

    def test_malformed_toml_names_the_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not valid TOML"):
            load(tmp_path, "[viafoura\nsection_uuid = ")


class TestBadValues:
    """Every message must name the key path, because that is the whole fix."""

    def test_a_missing_key_is_named(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"`article\.max_words`"):
            load(tmp_path, VALID.replace("max_words = 600", ""))

    def test_a_missing_section_is_named(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"`paths`"):
            load(tmp_path, VALID.split("[paths]")[0])

    def test_the_wrong_type_is_named_with_what_was_expected(
        self,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(
            ConfigError,
            match=r"`article\.max_words` must be an integer",
        ):
            load(tmp_path, VALID.replace("max_words = 600", 'max_words = "600"'))

    def test_a_boolean_is_not_an_integer(self, tmp_path: Path) -> None:
        """`bool` subclasses `int` in Python; `true` is not a count of anything."""
        with pytest.raises(ConfigError, match=r"`article\.max_words`"):
            load(tmp_path, VALID.replace("max_words = 600", "max_words = true"))

    def test_an_integer_is_accepted_where_a_number_is_wanted(
        self,
        tmp_path: Path,
    ) -> None:
        """`timeout_seconds = 30` and `= 30.0` must mean the same thing."""
        config = load(
            tmp_path,
            VALID.replace("timeout_seconds = 30.0", "timeout_seconds = 30", 1),
        )
        assert config.viafoura.timeout_seconds == 30.0

    def test_a_value_below_its_minimum_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"`viafoura\.page_size`"):
            load(tmp_path, VALID.replace("page_size = 100", "page_size = 0"))

    def test_a_negative_probability_is_refused(self, tmp_path: Path) -> None:
        """A negative floor excludes nothing while looking like a deliberate setting."""
        with pytest.raises(
            ConfigError,
            match=r"`classification\.on_topic_exclude_below`",
        ):
            load(
                tmp_path,
                VALID.replace(
                    "on_topic_exclude_below = 0.35",
                    "on_topic_exclude_below = -0.35",
                ),
            )

    def test_a_sweet_spot_of_the_wrong_shape_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"`classification\.sweet_spot`"):
            load(tmp_path, VALID.replace("sweet_spot = [20, 100]", "sweet_spot = [20]"))

    def test_a_sweet_spot_in_the_wrong_order_is_refused(self, tmp_path: Path) -> None:
        """[100, 20] matches nothing at all, silently."""
        with pytest.raises(ConfigError, match="ascending"):
            load(
                tmp_path,
                VALID.replace("sweet_spot = [20, 100]", "sweet_spot = [100, 20]"),
            )

    def test_cors_origins_must_be_strings(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"`api\.cors_origins`"):
            load(
                tmp_path,
                VALID.replace(
                    'cors_origins = ["http://localhost:3000"]',
                    "cors_origins = [3000]",
                ),
            )


class TestQuestionIds:
    """The guard that exists because the failure it prevents is silent.

    A threshold naming a question that does not exist is applied to nothing: the comment
    it was meant to exclude is proposed, no error is raised, and nothing in the report
    says so. It is the same class of failure as a renamed question reusing stale stored
    answers, one layer up.
    """

    @pytest.mark.parametrize("section", ["exclude_at", "flag_at"])
    def test_an_unknown_question_id_is_refused(
        self,
        section: str,
        tmp_path: Path,
    ) -> None:
        body = VALID.replace(
            f"[classification.{section}]\nsarcasm",
            f"[classification.{section}]\nsarcsam",  # a plausible typo
        )
        with pytest.raises(ConfigError, match="sarcsam"):
            load(tmp_path, body)

    def test_the_message_lists_the_ids_that_do_exist(self, tmp_path: Path) -> None:
        """Naming the typo without naming the alternatives leaves the reader guessing."""
        body = VALID.replace(
            "[classification.exclude_at]\nsarcasm",
            "[classification.exclude_at]\nsarcsam",
        )
        with pytest.raises(ConfigError) as caught:
            load(tmp_path, body)
        assert "sarcasm" in str(caught.value)
        assert "personal_attack" in str(caught.value)

    def test_every_real_question_id_is_accepted(self, tmp_path: Path) -> None:
        """The guard must not reject the battery it is guarding."""
        rows = "\n".join(f"{qid} = 0.5" for qid in sorted(QUESTION_IDS))
        body = VALID.replace(
            "[classification.exclude_at]\nsarcasm = 0.70",
            f"[classification.exclude_at]\n{rows}",
        )
        assert set(load(tmp_path, body).classification.exclude_at) == QUESTION_IDS


class TestWeights:
    """The weight names are fixed by `quality_score`, so the set must match exactly."""

    def test_a_missing_weight_is_refused(self, tmp_path: Path) -> None:
        """Missing, it would silently score every comment low."""
        with pytest.raises(ConfigError, match="missing tone"):
            load(tmp_path, VALID.replace("tone = 0.15\n", "", 1))

    def test_an_unknown_weight_is_refused(self, tmp_path: Path) -> None:
        """Present but unused, it would look like it was doing something."""
        with pytest.raises(ConfigError, match="unknown politeness"):
            load(
                tmp_path,
                VALID.replace("tone = 0.15", "tone = 0.15\npoliteness = 0.2"),
            )

    def test_weights_are_read_only(self, tmp_path: Path) -> None:
        """Policy is fixed at startup; nothing downstream may edit it mid-run."""
        weights = load(tmp_path).classification.weights
        with pytest.raises(TypeError):
            weights["tone"] = 0.9  # type: ignore[index]

    def test_file_order_is_preserved(self, tmp_path: Path) -> None:
        """The report prints these in the order the file lists them."""
        assert list(load(tmp_path).classification.weights) == [
            "experience",
            "tone",
            "readability",
            "contribution",
            "standalone",
            "representativeness",
        ]


class TestTheShippedFile:
    def test_the_shipped_config_is_valid(self) -> None:
        """The file in the repository must parse and pass every guard above."""
        config = load_config(CONFIG_PATH, known_question_ids=QUESTION_IDS)
        assert config.article.max_words > 0

    def test_the_shipped_weights_sum_to_one(self) -> None:
        """Each part is on 0-1 before weighting, so the score is only on 0-1 if these
        are. Nothing enforces it at parse time — a deliberate imbalance is legitimate
        while calibrating — so it is asserted here instead.
        """
        config = load_config(CONFIG_PATH, known_question_ids=QUESTION_IDS)
        assert sum(config.classification.weights.values()) == pytest.approx(1.0)

    def test_the_shipped_flag_thresholds_sit_below_the_exclusions(self) -> None:
        """A flag at or above its exclusion could never fire: the comment is gone first."""
        config = load_config(CONFIG_PATH, known_question_ids=QUESTION_IDS)
        for qid, flag_at in config.classification.flag_at.items():
            exclude_at = config.classification.exclude_at.get(qid)
            if exclude_at is not None:
                assert flag_at < exclude_at, f"{qid} can never be flagged"

    def test_the_shipped_api_binds_to_loopback(self) -> None:
        """The committed default must not be reachable from off the machine; a
        deployment moves it with HOST rather than by editing this file.
        """
        config = load_config(CONFIG_PATH, known_question_ids=QUESTION_IDS)
        assert config.api.host == "127.0.0.1"


@pytest.fixture
def _clean_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate `load_settings` from the developer's own `.env` and leave no global set.

    `load_dotenv` is stubbed out rather than allowed to run: a test that passes only on
    a machine with a populated `.env` is not a test, and this suite must never depend on
    the contents of that file.
    """
    monkeypatch.setattr(settings_module, "load_dotenv", lambda: None)
    for name in (
        "CAPI_URL",
        "CONTENT_READER_APIGEE_KEY",
        "TYPESAFE_API_KEY",
        "jev_api_key",
        "ENVIRONMENT",
        "HOST",
        "PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings_module, "_settings", None)


@pytest.fixture
def _env(monkeypatch: pytest.MonkeyPatch, _clean_settings: None) -> None:
    monkeypatch.setenv("CAPI_URL", "https://capi.invalid")
    monkeypatch.setenv("CONTENT_READER_APIGEE_KEY", "not-a-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-key")


@pytest.mark.usefixtures("_env")
class TestLoadSettings:
    def test_both_sources_arrive_on_one_object(self, tmp_path: Path) -> None:
        settings = load_settings(reload=True, config_path=write_config(tmp_path))
        assert settings.capi_url == "https://capi.invalid"  # from the environment
        assert settings.article.max_words == 600  # from the file

    def test_missing_variables_are_all_named_at_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One error, one fix. Naming them one at a time makes four runs of the same
        frustration.
        """
        monkeypatch.delenv("CAPI_URL")
        monkeypatch.delenv("TYPESAFE_API_KEY")
        with pytest.raises(SettingsError) as caught:
            load_settings(reload=True, config_path=write_config(tmp_path))
        assert "CAPI_URL" in str(caught.value)
        assert "TYPESAFE_API_KEY" in str(caught.value)

    def test_a_bad_config_file_raises_a_settings_error(self, tmp_path: Path) -> None:
        """One exception type at the startup boundary, whichever source was wrong."""
        path = write_config(tmp_path, VALID.replace("max_words = 600", ""))
        with pytest.raises(SettingsError, match=r"`article\.max_words`"):
            load_settings(reload=True, config_path=path)

    def test_the_repr_does_not_leak_a_secret(self, tmp_path: Path) -> None:
        """A traceback or a stray log line must not carry an API key with it."""
        settings = load_settings(reload=True, config_path=write_config(tmp_path))
        assert "not-a-key" not in repr(settings)
        assert "redacted" in repr(settings)

    def test_get_settings_fails_before_load(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings_module, "_settings", None)
        with pytest.raises(SettingsError, match="have not been loaded"):
            get_settings()


@pytest.mark.usefixtures("_env")
class TestHostAndPortOverride:
    """The one documented exception to "no key appears in both sources".

    Cloud Run injects `PORT` and will not route to a container bound to loopback, so a
    deployment must be able to move both without editing a committed file.
    """

    def test_the_file_wins_when_the_environment_is_silent(self, tmp_path: Path) -> None:
        settings = load_settings(reload=True, config_path=write_config(tmp_path))
        assert (settings.api.host, settings.api.port) == ("127.0.0.1", 8000)

    def test_the_environment_overrides_both(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOST", "0.0.0.0")  # noqa: S104 - the point of the test
        monkeypatch.setenv("PORT", "8080")
        settings = load_settings(reload=True, config_path=write_config(tmp_path))
        assert (settings.api.host, settings.api.port) == ("0.0.0.0", 8080)  # noqa: S104

    def test_one_may_be_overridden_without_the_other(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PORT", "8080")
        settings = load_settings(reload=True, config_path=write_config(tmp_path))
        assert (settings.api.host, settings.api.port) == ("127.0.0.1", 8080)

    @pytest.mark.parametrize("bad", ["eight thousand", "80.5", "0x1f90", "8000;rm"])
    def test_an_unparseable_port_stops_startup(
        self,
        bad: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Falling back to the file's port would bind somewhere the platform is not
        listening, and the failure would surface as "no traffic" much later.
        """
        monkeypatch.setenv("PORT", bad)
        with pytest.raises(SettingsError, match="PORT"):
            load_settings(reload=True, config_path=write_config(tmp_path))

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_variable_counts_as_unset(
        self,
        blank: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An exported-but-empty variable is a mistake, not an instruction to bind to
        nothing. The same rule keeps a key made of spaces out of `Settings`.
        """
        monkeypatch.setenv("PORT", blank)
        monkeypatch.setenv("HOST", blank)
        settings = load_settings(reload=True, config_path=write_config(tmp_path))
        assert (settings.api.host, settings.api.port) == ("127.0.0.1", 8000)

    @pytest.mark.parametrize("bad", ["0", "65536", "-1"])
    def test_a_port_outside_the_valid_range_stops_startup(
        self,
        bad: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PORT", bad)
        with pytest.raises(SettingsError, match="PORT"):
            load_settings(reload=True, config_path=write_config(tmp_path))


def test_parse_config_needs_no_file(tmp_path: Path) -> None:
    """The parser is separable from the filesystem, which is what lets the API validate
    a configuration before writing one.
    """
    sections = ("viafoura", "jev", "article", "classification", "api", "paths")
    raw = {name: {} for name in sections}
    # Every section present, every value absent: the first missing key is named, with
    # its section, which is all a reader needs to fix one at a time.
    with pytest.raises(ConfigError, match=r"missing the key `[a-z_]+\.[a-z_]+`"):
        parse_config(raw, root=tmp_path, known_question_ids=QUESTION_IDS)
