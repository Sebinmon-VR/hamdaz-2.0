"""Workflows: a team's process as arranged blocks, run for one task at a time.

``catalogue.py`` names the blocks and ships the presales flow; ``steps.py``
does what each block does; ``engine.py`` walks a run through them;
``worker.py`` wakes the runs that are waiting on the world.
"""
