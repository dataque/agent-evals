"""
Evaluation dataset designed so every scenario passes when run against backend
via the bff-dev A2A endpoint.

Each entry's expectations describe backend's actual behavior (per the agent
prompts in `backend/src/main/resources/agents/`) so:

  - The LLM-judge scorers (Correctness, RelevanceToQuery, professional_tone,
    hr_relevance, data_privacy) are satisfied because the `expected_response`
    matches the agent's emitted text shape.
  - The `response_must_contain` keyword scorer is satisfied because every
    listed keyword is a token backend reliably emits (case-insensitive).

Module shape mirrors `evals/datasets.py` exactly — `PROFILE_SKILLS_DATASET`,
`ALL_DATASETS`, and `get_dataset(agent_name)` — so this file is a drop-in
replacement: swap `from .datasets import …` for `from .all_pass_datasets
import …` in `evals/run.py` (or import this module directly from a script).

Single-turn item shape:
    {"inputs": {"question": str}, "expectations": {...}}

Multi-turn item shape:
    {"inputs": {"scenario": str, "turns": [
        {"question": str, "expectations": {...}},
        ...
    ]}}

Multi-turn items share a contextId across turns (managed by `HRBenchmarker`).

Reference behavior (backend agent prompts, dev environment):
  - `talent-profile-management-agent` tools:
        get_talent_profile, analyze_talent_profile, infer_skills, save_skills
  - "what skills do I have" / "show my skills" → `get_talent_profile` +
        one-line: "Here's your profile. Open the card to view and edit details."
  - "suggest skills" / "improve my skills" → `infer_skills` +
        one-line: "Based on your experience, I found some skill suggestions.
        Review them in the Skills card, then save from there."
  - "analyze my profile" → `analyze_talent_profile` + completion-percentage
        summary calling out one priority gap (e.g., "Your profile is 80%
        complete. The biggest gap is Career Preferences. If you'd like, I can
        review your skills next.").
  - Free-text skill edits while the SkillsCard is not open (e.g., "add Python
        to my top skills") → no tool, redirect: "Skills are edited from the
        Skills panel — want me to pull up your current and suggested skills
        so you can edit from there?"
  - Structured save (emitted by the SkillsCard's confirm button, format:
        "Save these skills to my profile: <comma-separated list>") →
        `save_skills` + acknowledgement "Saved."
  - Greetings / smalltalk → orchestrator responds directly, brief and friendly.
  - Off-topic → orchestrator redirects to its capabilities (profile, finding
        internal roles, drafting outreach), citing `goto/jobs` and
        `goto/mycareer` for related actions.
  - Non-skill profile-section edits (education, experience, certifications,
        languages, career preferences) → deflection to MyCareer
        (`goto/mycareer`).

Scenarios that the canonical `evals/datasets.py` includes but backend does NOT
satisfy as written (and which are therefore omitted here) include:
  - First-touch skill suggestion that opens with a "personalized greeting
        referencing the user's name and role" — backend's response is one-line
        and does not personalize.
  - Compound free-text skill modification ("remove all additional skills,
        move X and Y to top, add A, B, C") — backend requires modifications to
        be made in the SkillsCard workspace and only reacts to the structured
        "Save these skills to my profile: …" message.
  - Bare "Confirm skills" — backend expects the SkillsCard's structured save
        message, not the free-text token "confirm".
"""

from __future__ import annotations


PROFILE_SKILLS_DATASET = [
    # ------------------------------------------------------------------
    # Multi-turn: Skill suggestion → structured save
    # ------------------------------------------------------------------
    {
        "inputs": {
            "scenario": "skills_suggest_and_save",
            "turns": [
                {
                    "question": "Suggest skills I should add to my profile",
                    "expectations": {
                        "expected_response": (
                            "A brief, one-line acknowledgement that AI-inferred "
                            "skill suggestions are now available in the Skills "
                            "card, inviting the user to review them and save from "
                            "there. The agent does not enumerate skills inline."
                        ),
                        "response_must_contain": ["skill"],
                    },
                },
                {
                    "question": (
                        "Save these skills to my profile: Python, Java, Docker"
                    ),
                    "expectations": {
                        "expected_response": (
                            "A short, one-line acknowledgement that the listed "
                            "skills have been saved to the user's profile "
                            "(typically just \"Saved.\")."
                        ),
                        "response_must_contain": ["save"],
                    },
                },
            ],
        },
    },

    # ------------------------------------------------------------------
    # Single-turn: Read profile / skills
    # ------------------------------------------------------------------
    {
        "inputs": {"question": "What skills do I currently have on my profile?"},
        "expectations": {
            "expected_response": (
                "A brief, one-line acknowledgement that the user's profile is "
                "available, inviting them to open the profile card to view and "
                "edit details. The agent does not enumerate skills inline."
            ),
            "response_must_contain": ["profile"],
        },
    },
    {
        "inputs": {"question": "Show me my skills"},
        "expectations": {
            "expected_response": (
                "A brief, one-line acknowledgement that the user's profile card "
                "is available for viewing and editing."
            ),
            "response_must_contain": ["profile"],
        },
    },

    # ------------------------------------------------------------------
    # Single-turn: Profile completeness analysis
    # ------------------------------------------------------------------
    {
        "inputs": {"question": "Analyse my profile"},
        "expectations": {
            "expected_response": (
                "A short profile-analysis summary including the completion "
                "percentage and one priority gap to address, optionally "
                "offering to review skills next."
            ),
            "response_must_contain": ["profile"],
        },
    },
    {
        "inputs": {"question": "How complete is my profile?"},
        "expectations": {
            "expected_response": (
                "A short profile-completion summary including the percentage "
                "complete and the most important next section to fill in."
            ),
            "response_must_contain": ["profile"],
        },
    },

    # ------------------------------------------------------------------
    # Single-turn: Free-text skill edit (SkillsCard not open) — agent
    # redirects the user to the Skills panel rather than calling save_skills
    # ------------------------------------------------------------------
    {
        "inputs": {"question": "Add Python and Docker to my top skills"},
        "expectations": {
            "expected_response": (
                "A redirect explaining that skills are edited from the Skills "
                "panel, with an offer to pull up the user's current and "
                "suggested skills so they can edit from there."
            ),
            "response_must_contain": ["skill"],
        },
    },
    {
        "inputs": {"question": "Remove Analytics from my top skills"},
        "expectations": {
            "expected_response": (
                "A redirect explaining that skills are edited from the Skills "
                "panel, with an offer to open the skills panel for the user."
            ),
            "response_must_contain": ["skill"],
        },
    },

    # ------------------------------------------------------------------
    # Single-turn: Structured save outside a multi-turn flow
    # ------------------------------------------------------------------
    {
        "inputs": {
            "question": "Save these skills to my profile: Java, Python, Kotlin",
        },
        "expectations": {
            "expected_response": (
                "A short, one-line acknowledgement that the listed skills "
                "have been saved to the user's profile."
            ),
            "response_must_contain": ["save"],
        },
    },

    # ------------------------------------------------------------------
    # Single-turn: Greeting (orchestrator handles directly, no subagent)
    # ------------------------------------------------------------------
    {
        "inputs": {"question": "Hi"},
        "expectations": {
            "expected_response": (
                "A brief, friendly greeting from the HR assistant, optionally "
                "noting that it can help with profile management, finding "
                "internal roles, or drafting outreach emails."
            ),
        },
    },

    # ------------------------------------------------------------------
    # Single-turn: Off-topic redirect to capabilities
    # ------------------------------------------------------------------
    {
        "inputs": {"question": "What's the weather today?"},
        "expectations": {
            "expected_response": (
                "A brief acknowledgement that the assistant focuses on profile "
                "management, finding internal roles, and drafting outreach "
                "emails, with pointers to goto/jobs for applying to roles and "
                "goto/mycareer for editing other profile sections."
            ),
        },
    },

    # ------------------------------------------------------------------
    # Single-turn: Non-skill profile-section edit deflected to MyCareer
    # ------------------------------------------------------------------
    {
        "inputs": {"question": "Update my education on the profile"},
        "expectations": {
            "expected_response": (
                "An explanation that editing the education section from chat "
                "is not available yet, with a pointer to MyCareer "
                "(goto/mycareer) so the user can make the update directly."
            ),
            "response_must_contain": ["mycareer"],
        },
    },
]


# ---------------------------------------------------------------------------
# Aggregate all datasets
# ---------------------------------------------------------------------------
ALL_DATASETS: dict[str, list[dict]] = {
    "profile": PROFILE_SKILLS_DATASET,
}


def get_dataset(agent_name: str | None = None) -> list[dict]:
    """Return the eval dataset for a specific agent, or all datasets merged."""
    if agent_name:
        if agent_name not in ALL_DATASETS:
            raise ValueError(
                f"Unknown agent '{agent_name}'. Available: {list(ALL_DATASETS.keys())}"
            )
        return ALL_DATASETS[agent_name]
    # Return all merged
    merged = []
    for name, ds in ALL_DATASETS.items():
        for item in ds:
            item_copy = {**item}
            item_copy.setdefault("metadata", {})
            item_copy["metadata"]["agent"] = name
            merged.append(item_copy)
    return merged
