"""siphonophore-harness -- the minimal native cognitive loop DESIGN.md section 6 names: prompt ->
completion -> parse intent -> feed result back. Owned and minimal (DESIGN.md section 0): no
external agent SDK, no provider client library, nothing borrowed from this project's own prior
architecture.

Deliberately provisional as its own distribution (DESIGN.md section 6 calls the whole package
layout "deliberately provisional"): this lives alongside siphonophore_core in the same repo and
distribution for now rather than as a genuinely separate installable package with its own
pyproject.toml and declared dependency on siphonophore-core. Splitting it out is mechanical
whenever that separation actually earns its cost -- nothing in this package's own code assumes the
monorepo layout.

    Model            -- the loop's only source of new intent (model.py)
    parse_intent      -- the ONLY place untrusted completion text becomes an Intent (intent_parsing.py)
    Broker            -- the ONLY capability a CognitiveLoop holds that can produce an Effect (broker.py)
    CognitiveLoop     -- prompt -> completion -> parse intent -> Broker.dispatch -> feed back (loop.py)
"""
