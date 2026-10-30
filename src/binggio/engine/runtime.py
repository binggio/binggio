# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Binggio v16: the only interpretation path for Ask Binggio.

Errors never return None. There is no fallback to Classic, legacy_session,
conversation_v3, or a second NL parser. Existing approved read-only source
readers remain execution adapters, not competing interpreters.
"""
from __future__ import annotations
import logging
import re
from binggio.engine import core

log = logging.getLogger('binggio.unified')

# v17.1: only genuine references to an earlier result. "above 8,000,000",
# "their latest assessment" and "previous assessment" are NOT references.
REFERENCE = re.compile(r'\b(?:these|those|them|of the above|the above (?:list|results?|answer|rows|ones|loans|customers|accounts|complaints|items)'
                       r'|previous (?:results?|answer|list|query)|earlier (?:results?|answer|list|query)|last (?:results?|answer|list|query)'
                       r'|that set|this set|this cohort|same (?:ones|set|list|group))\b', re.I)


class PipelineFailure(ValueError):
    def __init__(self, stage, code, message, hints=None):
        self.stage, self.code = stage, code
        # v17: answerability hints - what is missing (for example an unapproved
        # column whose values match the question). Shown to the user; never
        # used to change the decision and never sent to the model.
        self.hints = list(hints or [])
        super().__init__(message)


def _hints(project, universe, question):
    try:
        return core.answerability_hints(project, universe, question)
    except Exception:
        log.exception('Answerability hints failed (non-fatal)')
        return []


def execute_question(project, session_id, question, referenced_turn_id=None, canonical_plan=None):
    """Canonical plan -> governed execution -> optional verified cohort.

    The reference is loaded only for genuine follow-ups. No implicit use of a
    previous cohort for a fresh standalone question. All failures are explicit.
    """
    from binggio.context.context import approved_context
    from binggio.engine import sessions as store

    uses_reference=bool(REFERENCE.search(question)) or bool(referenced_turn_id)
    prior=None
    if uses_reference:
        try:prior=store.current_cohort(project,session_id,referenced_turn_id)
        except store.SessionConflict as e:raise PipelineFailure('context','COHORT_NOT_AVAILABLE',str(e)) from e
        if prior is None:
            raise PipelineFailure('context','COHORT_NOT_AVAILABLE',
               'This question refers to previous results, but no complete verified cohort is saved in this conversation. Start a new conversation with the first query.')
        if prior.get('cohort_kind') not in ('phase7_approved','approved_entity'):
            raise PipelineFailure('context','COHORT_NOT_GOVERNED',
               'The previous result came from an older Classic-only path and cannot be reused as an approved cross-source cohort. Rerun the first question in a fresh v16 conversation.')
    try:
        ctx=approved_context(project)
        universe=core.Universe(ctx)
    except (core.Blocked, KeyError, ValueError) as e:
        if 'no tables or fields' in str(e):
            raise PipelineFailure('registry','NOTHING_APPROVED',
                'Nothing is approved yet, so there is nothing to answer from. Add a source, run profile and '
                'discover, then approve column roles and relationships (binggio review).') from e
        raise PipelineFailure('registry','REGISTRY_INVALID',str(e)) from e
    if canonical_plan is not None:
        try:
            plan=core.validate(canonical_plan,universe,prior)
            domains=core.observed_status_domains(project,universe)
            plan=core.validate_status_literals(plan,universe,domains)
        except core.Blocked as e:raise PipelineFailure('validation','PLAN_REJECTED',str(e)) from e
    else:
        if not core._llm_enabled():
            raise PipelineFailure('interpretation','MODEL_NOT_CONFIGURED',
                'Asking questions needs a model key. Set BINGGIO_LLM_API_KEY (or OPENAI_API_KEY) and try again.')
        unsupported_class=getattr(core,'UnsupportedClass',())
        try:plan=core.interpret(question,universe,prior,project=project)
        except core.InvalidCanonicalPlan as e:
            # A malformed plan is a technical failure: no semantic hints.
            raise PipelineFailure('interpretation','INVALID_CANONICAL_PLAN',str(e)) from e
        except core.Unrecognized as e:
            raise PipelineFailure('interpretation','MODEL_UNAVAILABLE',str(e)) from e
        except unsupported_class as e:
            raise PipelineFailure('interpretation','UNSUPPORTED_QUESTION_CLASS',str(e)) from e
        except core.Blocked as e:
            raise PipelineFailure('interpretation','UNSUPPORTED_OR_AMBIGUOUS',str(e),
                                  _hints(project,universe,question)) from e
    # Explicit core.validate enforces that a follow-up actually consumes its
    # referenced cohort. Without this, the model could silently drop "these".
    if uses_reference and plan.get('cohort')!='active':
        raise PipelineFailure('validation','CONTEXT_NOT_APPLIED',
            'Canonical plan omitted the explicitly referenced cohort. No query was executed.')
    if not uses_reference and plan.get('cohort')=='active':
        raise PipelineFailure('validation','UNREQUESTED_CONTEXT',
            'Canonical plan attempted to use an earlier cohort for a standalone question. No query was executed.')
    try:
        answer,cohort=core.execute(project,universe,plan,prior)
    except core.Blocked as e:
        raise PipelineFailure('execution','EXECUTION_REJECTED',str(e)) from e
    answer.setdefault('provenance',{})['query_pipeline']='v16_canonical_only'
    answer['pipeline']={'version':'v16','engine':getattr(core,'ENGINE_VERSION','v17.2'),'stage':'completed','interpreter':'canonical_only',
                        'source':'approved_registry','fallback_used':False}
    return answer,plan,cohort
