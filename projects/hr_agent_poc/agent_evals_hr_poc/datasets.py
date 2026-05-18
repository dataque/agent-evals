"""HR Agent PoC eval datasets.

Copied from chat-evals' ``evals/datasets.py`` (``PROFILE_SKILLS_DATASET``).
Multi-turn items share a thread_id across turns (MLflowRunner handles this).
"""

from __future__ import annotations

PROFILE_SKILLS_DATASET = [
    {
        "inputs": {
            "scenario": "skills_modify_and_confirm",
            "turns": [
                {
                    "question": "Suggest skills I should add to my profile",
                    "expectations": {
                        "expected_response": (
                            "A personalized greeting referencing the user's name and role, "
                            "followed by AI-generated skill suggestions categorized into "
                            "top skills and additional skills. Includes a prompt to review "
                            "the generated skills and add at least 5 top skills for role suggestions."
                        ),
                        "response_must_contain": ["skill"],
                    },
                },
                {
                    "question": (
                        "Remove all the additional skills, move P&L and Analytical "
                        "thinking to top skills, and add Java, javascript and react "
                        "to top skills"
                    ),
                    "expectations": {
                        "expected_response": (
                            "Confirmation that the skills have been updated. All additional "
                            "skills removed. P&L and Analytical Thinking moved to top skills. "
                            "Java, Javascript, and React added to top skills. Updated skill "
                            "count shown."
                        ),
                        "response_must_contain": ["skill"],
                    },
                },
                {
                    "question": "Confirm skills",
                    "expectations": {
                        "expected_response": (
                            "Skills saved to the user's profile. Confirmation message "
                            "indicating skills have been added. Agent transitions to "
                            "looking for relevant open roles."
                        ),
                        "response_must_contain": ["confirm", "profile"],
                    },
                },
            ],
        },
    },
    {
        "inputs": {"question": "Add Python and Docker to my top skills"},
        "expectations": {
            "expected_response": (
                "Confirmation that Python and Docker have been added to the "
                "user's top skills. Updated skill list shown."
            ),
            "response_must_contain": ["Python", "Docker"],
        },
    },
    {
        "inputs": {"question": "What skills do I currently have on my profile?"},
        "expectations": {
            "expected_response": (
                "A list of the user's current skills, categorized into top skills "
                "and additional skills, with the total count."
            ),
            "response_must_contain": ["skill"],
        },
    },
    {
        "inputs": {"question": "Remove Analytics from my top skills"},
        "expectations": {
            "expected_response": (
                "Confirmation that Analytics has been removed from the user's "
                "top skills. Updated skill list shown."
            ),
            "response_must_contain": ["Analytics"],
        },
    },
    {
        "inputs": {"question": "Analyse my profile"},
        "expectations": {
            "expected_response": (
                "The user's MyCareer profile is not set up. Profile strength "
                "is 0% / Not started. Prompt to set up the profile via MyCareer. "
                "Mentions it only takes 5 minutes with a CV upload."
            ),
            "response_must_contain": ["profile"],
        },
    },
    {
        "inputs": {"question": "How complete is my profile?"},
        "expectations": {
            "expected_response": (
                "Profile completion status with a percentage score. If profile "
                "is incomplete, guidance on what to add to improve the score."
            ),
            "response_must_contain": ["profile"],
        },
    },
    {
        "inputs": {
            "scenario": "confirm_existing_skills_and_match",
            "turns": [
                {
                    "question": "Show me my skills",
                    "expectations": {
                        "expected_response": (
                            "Display of the user's current skills categorized into "
                            "top skills and additional skills."
                        ),
                        "response_must_contain": ["skill"],
                    },
                },
                {
                    "question": "Confirm skills",
                    "expectations": {
                        "expected_response": (
                            "Skills confirmed and saved to profile. Agent automatically "
                            "transitions to finding relevant open roles. Returns matched "
                            "roles with job titles, levels, divisions, and locations."
                        ),
                        "response_must_contain": ["profile"],
                    },
                },
            ],
        },
    },
]


ALL_DATASETS: dict[str, list[dict]] = {
    "profile": PROFILE_SKILLS_DATASET,
}
