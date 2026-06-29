import unittest

import hermes_headroom_plugin


class FakeCtx:
    def __init__(self):
        self.tools = []
        self.commands = []
        self.hooks = []
        self.middleware = []
        self.skills = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_command(self, *args, **kwargs):
        self.commands.append((args, kwargs))

    def register_hook(self, *args):
        self.hooks.append(args)

    def register_middleware(self, *args):
        self.middleware.append(args)

    def register_skill(self, *args, **kwargs):
        self.skills.append((args, kwargs))


class MinimalCtx:
    def __init__(self):
        self.tools = []
        self.commands = []
        self.hooks = []
        self.skills = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_command(self, *args, **kwargs):
        self.commands.append((args, kwargs))

    def register_hook(self, *args):
        self.hooks.append(args)

    def register_skill(self, *args, **kwargs):
        self.skills.append((args, kwargs))


class RegisterTest(unittest.TestCase):
    def test_register_core_surface(self):
        ctx = FakeCtx()
        hermes_headroom_plugin.register(ctx)
        self.assertEqual(ctx.tools[0]["name"], "headroom_retrieve")
        self.assertEqual(ctx.tools[0]["toolset"], "headroom")
        self.assertNotIn("check_fn", ctx.tools[0])
        self.assertEqual(ctx.commands[0][0][0], "headroom")
        self.assertIn(("llm_request", ctx.middleware[0][1]), ctx.middleware)
        self.assertTrue(ctx.skills)

    def test_register_without_middleware_support(self):
        ctx = MinimalCtx()
        hermes_headroom_plugin.register(ctx)
        self.assertEqual(ctx.tools[0]["name"], "headroom_retrieve")
        self.assertEqual(ctx.commands[0][0][0], "headroom")
        self.assertTrue(ctx.skills)


if __name__ == "__main__":
    unittest.main()
