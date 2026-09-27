## What this stage is

Model output, sold by the million tokens. Labs train models on very large clusters and then serve them, and serving is where the volume is. Its unit is the token, and the two numbers that describe the stage are how many tokens a GPU produces in an hour and what a million of them sells for.

## How it works

Throughput per GPU depends on the model's size, the hardware generation and the serving software, and it has risen far faster than hardware alone would explain. Price per million tokens has fallen faster still, driven by competition and by cheaper serving. Training is a one-off cost per model generation, large and lumpy; inference is a running cost that scales with use. Public price pages give the selling price; vendor benchmarks give throughput; the two together give revenue per GPU-hour, which can be set against the cost of that hour from the stages below.

Lab revenue and token volumes are disclosed irregularly and often through the press, so this stage has the widest error bars in the chain.

## What to watch

- Tokens per second per GPU for a named model on a named chip.
- Price per million tokens for the leading models, input and output separately.
- Tokens processed per day where a platform discloses it.
- Training run size and cost by generation.
- Lab revenue run-rates, with the sourcing caveat attached.

## How it connects

Tokens per GPU-hour times price per million tokens is what the GPU-hour earns in the hands of a lab. Applications above decide how many tokens the world wants and what it will pay.
