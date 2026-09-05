// Run: node --experimental-vm-modules --test tests/admin_commands.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import vm from "node:vm";

const source = resolve("addon_src/RestartManagerLink/scripts");
async function fixture(reference) {
    const logs = [], broadcasts = [], tasks = [], commands = new Map(), answers = [], forms = [], events = {};
    const manager = { reply: true, payload: "06:00|06:00|1h", onReply: undefined };
    const signal = name => ({ subscribe(fn) { (events[name] ??= []).push(fn); } });
    const system = {
        currentTick: 100, beforeEvents: { startup: signal("startup") },
        afterEvents: { scriptEventReceive: signal("script") },
        run(fn) { tasks.push(fn); }, runTimeout(fn) { tasks.push(fn); },
        async waitTicks(ticks) {
            system.currentTick += ticks;
            await Promise.resolve();
            const ready = tasks.splice(0);
            for (const task of ready) task();
        },
    };
    const dim = { id: "minecraft:overworld", commands: [], runCommand(cmd) { this.commands.push(cmd); return { successCount: 1 }; } };
    const players = [];
    const props = new Map();
    const world = {
        time: 0, afterEvents: { playerLeave: signal("leave"), worldLoad: signal("load") },
        getAllPlayers: () => players,
        getDimension(id) { assert.ok(["minecraft:overworld", "overworld", "minecraft:nether"].includes(id)); return { ...dim, id }; },
        setTimeOfDay(time) { world.time = time; },
        sendMessage(message) { broadcasts.push(message); },
        getDynamicProperty: key => props.get(key), setDynamicProperty: (key, value) => props.set(key, value),
    };
    function makePlayer(id, name, operator = false) {
        const dynamic = new Map();
        const components = new Map(["health", "player.hunger", "player.saturation", "player.exhaustion"].map(k => ["minecraft:" + k, {
            currentValue: 5, effectiveMax: 20, resetToMaxValue() { this.currentValue = 20; }, resetToMinValue() { this.currentValue = 0; },
        }]));
        const player = {
            id, name, typeId: "minecraft:player", isValid: true, playerPermissionLevel: operator ? 2 : 1,
            location: { x: id === "a" ? 1 : 20, y: 64, z: 0 }, dimension: dim, messages: [], teleports: [], commands: [],
            getRotation: () => ({ x: 0, y: 90 }), getGameMode: () => "Survival",
            sendMessage(message) { this.messages.push(message); },
            getComponent: id => components.get(id), components,
            getDynamicProperty: key => dynamic.get(key), setDynamicProperty: (key, value) => dynamic.set(key, value), dynamic,
            tryTeleport(location, options) {
                this.teleports.push({ location, options });
                if (this.blocked) return false;
                this.location = { ...location }; this.dimension = options.dimension; return true;
            },
            runCommand(command) { this.commands.push(command); return { successCount: 1 }; },
            hasTag: () => false, addTag() {}, removeTag() {},
        };
        players.push(player);
        return player;
    }
    const admin = makePlayer("a", "Admin", true), member = makePlayer("b", "Player Two");
    class Form {
        constructor() { this.buttons = []; }
        title(value) { this.heading = value; return this; }
        body(value) { this.text = value; return this; }
        button(value) { this.buttons.push(value); return this; }
        button1(value) { this.buttons[0] = value; return this; }
        button2(value) { this.buttons[1] = value; return this; }
        textField(...args) { this.field = args; return this; }
        async show(player) {
            forms.push(this);
            const answer = answers.shift() ?? { canceled: true };
            return typeof answer === "function" ? answer(player, this) : answer;
        }
    }
    const api = { system, world, ScriptEventSource: { Server: "Server", Entity: "Entity" },
        CommandPermissionLevel: { Admin: 2 }, PlayerPermissionLevel: { Operator: 2 },
        CustomCommandParamType: { PlayerSelector: "PlayerSelector", String: "String" },
        CustomCommandStatus: { Success: 0, Failure: 1 }, CustomCommandSource: { Entity: "Entity" } };
    const ui = { ActionFormData: Form, ModalFormData: Form, MessageFormData: Form, FormCancelationReason: { UserBusy: "UserBusy" } };
    const context = vm.createContext({ console: { warn: text => {
        logs.push(text);
        if (text === "[MGR]|sync|" && manager.reply) tasks.push(() => {
            manager.onReply?.();
            event("mgrback:info", manager.payload, undefined, { sourceType: "Server", sourceEntity: undefined });
        });
    }, error: text => logs.push(text) } });
    const synthetic = (exports, identifier) => new vm.SyntheticModule(Object.keys(exports), function () {
        for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context, identifier });
    const cache = new Map([
        ["@minecraft/server", synthetic(api, "server")], ["@minecraft/server-ui", synthetic(ui, "ui")],
    ]);
    if (reference) {
        const definitions = JSON.parse(readFileSync('command_reference.json', 'utf8'));
        cache.set('./reference.js', synthetic({ REFERENCE: reference,
            ADMIN_COMMANDS: definitions.admin.map(e => [e.name, e.summary, e.param || '']) }, 'reference'));
    }
    async function load(name) {
        if (cache.has(name)) return cache.get(name);
        const module = new vm.SourceTextModule(readFileSync(resolve(source, name.replace("./", "")), "utf8"), { context, identifier: name });
        cache.set(name, module);
        await module.link(load);
        return module;
    }
    const module = await load("./main.js");
    await module.evaluate();
    for (const fn of events.startup) fn({ customCommandRegistry: { registerCommand(definition, callback) {
        assert.ok(!commands.has(definition.name)); commands.set(definition.name, { definition, callback });
    } } });
    function command(name, value, player = admin, extra = {}) {
        return commands.get(`admin:${name}`).callback({ sourceType: "Entity", sourceEntity: player, ...extra }, value);
    }
    async function flush() {
        for (let i = 0; i < 8; i++) {
            while (tasks.length) tasks.shift()();
            await new Promise(resolve => setImmediate(resolve));
        }
    }
    function event(id, message = "", player = admin, extra = {}) {
        for (const fn of events.script) fn({ id, message, sourceType: "Entity", sourceEntity: player, ...extra });
    }
    return { admin, member, players, makePlayer, commands, command, event, flush, logs, broadcasts,
        world, dim, system, props, answers, forms, tasks, events, manager };
}

const choose = text => (_player, form) => {
    const index = form.buttons.findIndex(label => label.replace(/§./g, "").startsWith(text));
    assert.ok(index >= 0, `No '${text}' button in ${form.heading}: ${form.buttons.join(", ")}`);
    return { canceled: false, selection: index };
};

test("registers 16 namespaced, operator-only commands with typed parameters", async () => {
    const f = await fixture();
    assert.equal(f.commands.size, 16);
    for (const { definition } of f.commands.values()) {
        assert.equal(definition.permissionLevel, 2); assert.equal(definition.cheatsRequired, false);
    }
    assert.equal(f.commands.get("admin:heal").definition.optionalParameters[0].type, "PlayerSelector");
    assert.equal(f.commands.get("admin:bring").definition.mandatoryParameters[0].type, "PlayerSelector");
    assert.equal(f.commands.get("admin:announce").definition.mandatoryParameters[0].type, "String");
    assert.equal(f.commands.get("admin:help").definition.optionalParameters[0].type, "String");
    const definitions = JSON.parse(readFileSync('command_reference.json', 'utf8'));
    assert.deepEqual([...f.commands.keys()], definitions.admin.map(e => `admin:${e.name}`));
});

test("help searches console commands and sends documentation without executing anything", async () => {
    const f = await fixture();
    f.answers.push(choose('/stop'), (player, form) => {
        assert.match(form.text, /Server console only/);
        assert.match(form.text, /Example \(reference only\): stop/);
        return choose('Send reference to my chat')(player, form);
    }, choose('Back to results'), choose('Close help'));
    f.command('help', 'stop'); await f.flush();
    assert.ok(f.admin.messages.some(text => text.includes('Server console only')));
    assert.equal(f.admin.commands.length, 0); assert.equal(f.dim.commands.length, 0);
    assert.equal(f.broadcasts.length, 0); assert.equal(f.world.time, 0);
    assert.ok(!f.logs.some(line => line.includes('[MGR]|ev|')));
});

test("help supports category pagination, search defaults and empty results", async () => {
    const f = await fixture();
    f.answers.push(choose('Core server'), choose('Next page'), (player, form) => {
        assert.match(form.text, /Page 2\/3/);
        return choose('Search / change filter')(player, form);
    }, (_player, form) => {
        assert.equal(form.field[2].defaultValue, '');
        return { canceled: false, formValues: ['doesnotexist'] };
    }, (player, form) => {
        assert.match(form.text, /No matching entries/);
        return choose('Help home')(player, form);
    }, choose('Close help'));
    f.command('help'); await f.flush();
    assert.equal(f.answers.length, 0);
});

test("legacy help and toolbox reference both open the unified guide", async () => {
    const f = await fixture();
    f.event('mgr:help', 'weather'); await f.flush();
    assert.equal(f.forms.at(-1).heading, 'Search commands');
    assert.ok(f.forms.at(-1).buttons.some(label => label.startsWith('/weather')));
    f.answers.push(choose('Command reference'), choose('Close help'), choose('Close'));
    f.command('menu'); await f.flush();
    assert.ok(f.forms.some(form => form.heading === 'Server command help'));
    assert.equal(f.answers.length, 0);
});

test("help labels disabled mods and keeps discovered functions read-only", async () => {
    const reference = { notes: 'Read-only guide', warnings: [], packs: [
        { name: 'Example mod', status: 'Installed, disabled in world', count: 1 },
        { name: 'Archive only', status: 'Uninstalled / archive disabled', count: 0 }],
        entries: [{ title: '/function internal/tick', syntax: '/function internal/tick', category: 'Installed mods',
            pack: 'Example mod', status: 'Installed, disabled in world', summary: 'Internal function',
            evidence: 'Function file found; not verified as a public/admin command', example: '/help function', source: 'functions/internal/tick.mcfunction' }] };
    const f = await fixture(reference);
    f.answers.push((player, form) => {
        assert.match(form.text, /Uninstalled \/ archive disabled/);
        return choose('Installed mods')(player, form);
    }, choose('/function internal/tick'), (player, form) => {
        assert.match(form.text, /not verified as a public\/admin command/);
        assert.match(form.text, /Installed, disabled in world/);
        assert.ok(!form.buttons.some(text => /^(Run|Execute)/.test(text)));
        return choose('Close help')(player, form);
    });
    f.command('help'); await f.flush();
    assert.equal(f.dim.commands.length, 0); assert.equal(f.admin.commands.length, 0);
});

test("losing operator permission in help prevents follow-on forms and chat copying", async () => {
    const f = await fixture();
    f.answers.push((player, form) => {
        player.playerPermissionLevel = 1;
        return choose('Core server')(player, form);
    });
    f.command('help'); await f.flush();
    assert.equal(f.forms.length, 1);
    assert.equal(f.world.time, 0);
});

test("members, NPCs, command blocks and console cannot invoke player commands", async () => {
    const f = await fixture();
    for (const extra of [{ sourceType: "Server" }, { sourceType: "Block" }, { sourceType: "NPCDialogue" }, { initiator: f.member }, { sourceBlock: {} }]) {
        assert.equal(f.command("day", undefined, f.admin, extra).status, 1);
    }
    assert.equal(f.command("day", undefined, f.member).status, 1);
    await f.flush(); assert.equal(f.world.time, 0);
});

test("world changes are deferred and permissions are checked again", async () => {
    const f = await fixture();
    assert.equal(f.command("day").status, 0); assert.equal(f.world.time, 0);
    f.admin.playerPermissionLevel = 1;
    await f.flush(); assert.equal(f.world.time, 0);
    assert.match(f.admin.messages.join(), /operator/);
});

test("heal defaults to self; feed fills hunger, saturation and clears exhaustion", async () => {
    const f = await fixture();
    f.command("heal"); await f.flush();
    assert.equal(f.admin.getComponent("minecraft:health").currentValue, 20);
    assert.equal(f.member.getComponent("minecraft:health").currentValue, 5);
    f.system.currentTick += 20; f.command("feed", [f.member]); await f.flush();
    assert.equal(f.member.getComponent("minecraft:player.hunger").currentValue, 20);
    assert.equal(f.member.getComponent("minecraft:player.saturation").currentValue, 20);
    assert.equal(f.member.getComponent("minecraft:player.exhaustion").currentValue, 0);
    assert.ok(f.logs.some(line => line.includes('"k":"command"') && line.includes("/admin:feed Player Two")));
});

test("empty and multi-target selectors never fall back to self", async () => {
    const f = await fixture();
    for (const targets of [[], [f.member, f.admin]]) {
        f.system.currentTick += 20; f.command("heal", targets); await f.flush();
    }
    assert.equal(f.member.getComponent("minecraft:health").currentValue, 5);
    assert.equal(f.admin.getComponent("minecraft:health").currentValue, 5);
    assert.match(f.admin.messages.join(), /exactly one/);
});

test("disconnected targets and disconnected operators cannot act", async () => {
    const f = await fixture();
    f.command("heal", [f.member]); f.players.splice(1, 1); await f.flush();
    assert.equal(f.member.getComponent("minecraft:health").currentValue, 5);
    f.system.currentTick += 20; f.command("day"); f.players.splice(0); await f.flush();
    assert.equal(f.world.time, 0);
});

test("bring saves target's location, return restores it, and back swaps return points", async () => {
    const f = await fixture();
    f.member.dimension = { ...f.dim, id: "minecraft:nether" };
    f.command("bring", [f.member]); await f.flush();
    assert.equal(f.member.location.x, 1); assert.equal(f.member.teleports[0].options.checkForBlocks, true);
    f.system.currentTick += 20; f.command("return", [f.member]); await f.flush();
    assert.equal(f.member.location.x, 20); assert.equal(f.member.dimension.id, "minecraft:nether");
    f.system.currentTick += 20; f.command("goto", [f.member]); await f.flush();
    assert.equal(f.admin.location.x, 20);
    f.system.currentTick += 20; f.command("back"); await f.flush(); assert.equal(f.admin.location.x, 1);
});

test("failed teleport preserves previous return point and does not report success", async () => {
    const f = await fixture();
    f.member.dynamic.set("mgr:admin_return", "previous"); f.member.blocked = true;
    f.command("bring", [f.member]); await f.flush();
    assert.equal(f.member.dynamic.get("mgr:admin_return"), "previous");
    assert.equal(f.member.location.x, 20); assert.match(f.admin.messages.join(), /Teleport blocked/);
    assert.ok(f.logs.some(line => line.includes("FAILED")));
});

test("missing, invalid and self return/travel targets fail privately", async () => {
    const f = await fixture();
    f.command("back"); await f.flush(); assert.match(f.admin.messages.join(), /No saved return/);
    f.system.currentTick += 20; f.admin.dynamic.set("mgr:admin_return", '{"location":{"x":null}}');
    f.command("back"); await f.flush(); assert.match(f.admin.messages.join(), /invalid/);
    f.system.currentTick += 20; f.command("goto", [f.admin]); await f.flush();
    assert.match(f.admin.messages.join(), /different player/); assert.equal(f.broadcasts.length, 0);
});

test("announcements sanitize formatting, enforce limits and never run raw commands", async () => {
    const f = await fixture();
    f.command("announce", '§cHello\n/kill @a'); await f.flush();
    assert.equal(f.broadcasts.length, 1); assert.ok(!f.broadcasts[0].includes("\n"));
    assert.ok(!f.broadcasts[0].includes("§c")); assert.equal(f.dim.commands.length, 0);
    for (const value of ["", "a".repeat(181)]) {
        f.system.currentTick += 20; f.command("announce", value); await f.flush();
    }
    assert.equal(f.broadcasts.length, 1);
});

test("world controls and inspection report actual values", async () => {
    const f = await fixture();
    f.command("day"); await f.flush(); assert.equal(f.world.time, 1000);
    f.system.currentTick += 20; f.command("night"); await f.flush(); assert.equal(f.world.time, 13000);
    f.system.currentTick += 20; f.command("clearweather"); await f.flush(); assert.deepEqual(f.dim.commands, ["weather clear 600"]);
    f.system.currentTick += 20; f.command("inspect", [f.member]); await f.flush();
    assert.match(f.admin.messages.join(), /Health: §f5\/20/);
    assert.match(f.admin.messages.join(), /20, 64, 0/);
});

test("restart requires confirmation; cancellation keeps existing manager semantics", async () => {
    const f = await fixture();
    f.answers.push({ canceled: false, selection: 0 }); f.command("restart"); await f.flush();
    assert.ok(!f.logs.some(line => line.startsWith("[MGR]|restart|")));
    f.system.currentTick += 20; f.answers.push({ canceled: false, selection: 1 });
    f.command("restart"); await f.flush(); assert.ok(f.logs.includes("[MGR]|restart|"));
    f.system.currentTick += 20; f.command("cancelrestart"); await f.flush();
    assert.ok(f.logs.includes("[MGR]|cancel|"));
});

test("privilege revocation while any manager form is open prevents mutation", async () => {
    const f = await fixture();
    f.answers.push(() => { f.admin.playerPermissionLevel = 1; return { canceled: false, selection: 1 }; });
    f.command("restart"); await f.flush();
    assert.ok(!f.logs.includes("[MGR]|restart|"));
});

test("toolbox player picker routes the chosen player through the same command guard", async () => {
    const f = await fixture();
    f.answers.push(choose("Player care"), choose("Heal player"), choose("Player Two"));
    f.command("menu"); await f.flush();
    assert.equal(f.member.getComponent("minecraft:health").currentValue, 20);
    assert.equal(f.forms[0].buttons.length, 8);
    assert.match(f.forms.at(-1).text, /Player Two/);
});

test("busy chat menus retry; command spam is throttled; leave cleans the cooldown", async () => {
    const f = await fixture();
    f.answers.push({ canceled: true, cancelationReason: "UserBusy" }, { canceled: true });
    f.command("menu"); assert.equal(f.command("day").status, 1); await f.flush();
    assert.equal(f.forms.length, 2);
    for (const fn of f.events.leave) fn({ playerId: f.admin.id });
    assert.equal(f.command("day").status, 0); await f.flush(); assert.equal(f.world.time, 1000);
});

test("legacy mgr entry points are op-only and manager info rejects player spoofing", async () => {
    const f = await fixture();
    f.event("mgr:restart", "", f.member); f.event("mgr:menu", "", f.member);
    f.event("mgrback:info", "spoof|spoof|spoof", f.admin); await f.flush();
    assert.equal(f.props.size, 0); assert.equal(f.forms.length, 0); assert.ok(!f.logs.includes("[MGR]|restart|"));
    f.event("mgrback:info", "06:00|06:00|1h", undefined, { sourceType: "Server", sourceEntity: undefined });
    assert.equal(f.props.get("mgr:lastinfo"), "06:00|06:00|1h");
    f.event("mgr:restart", "bad\n[MGR]|restart|"); assert.ok(!f.logs.includes("[MGR]|restart|"));
});

test("failed private feedback is never broadcast to everyone", async () => {
    const f = await fixture();
    f.admin.sendMessage = () => { throw new Error("disconnected"); };
    f.command("help"); await f.flush(); assert.equal(f.broadcasts.length, 0);
});

test("toolbox can navigate to manager and back without a stale open-menu lock", async () => {
    const f = await fixture();
    f.answers.push(choose("Restart & mod controls"), choose("Admin Toolbox"), { canceled: true });
    f.command("menu"); await f.flush();
    assert.equal(f.forms.length, 3);
    assert.equal(f.forms[2].heading, "§aADMIN TOOLBOX");
});

test("logging an invalid disconnected player handle does not reject asynchronously", async () => {
    const f = await fixture();
    f.command("back"); f.players.splice(0, 1);
    Object.defineProperty(f.admin, "name", { get() { throw new Error("InvalidEntityError"); } });
    await f.flush();
    assert.equal(f.broadcasts.length, 0);
});

test("read-only help does not consume the first action cooldown", async () => {
    const f = await fixture();
    f.command("help"); await f.flush();
    assert.equal(f.command("heal").status, 0); await f.flush();
    assert.equal(f.admin.getComponent("minecraft:health").currentValue, 20);
    assert.ok(f.admin.messages.every(message => message.length < 700));
});

test("picker paginates large rosters, searches names and keeps targeting stable", async () => {
    const f = await fixture();
    for (let i = 0; i < 21; i++) f.makePlayer(`guest${i}`, `Guest ${String(i).padStart(2, "0")}`);
    f.answers.push(choose("Player care"), choose("Heal player"), choose("Next page"), choose("Search players"),
        { canceled: false, formValues: ["guest 19"] }, choose("Guest 19"));
    f.command("menu"); await f.flush();
    assert.equal(f.players.find(p => p.name === "Guest 19").getComponent("minecraft:health").currentValue, 20);
    const pickers = f.forms.filter(form => form.heading === "§aAdmin · heal");
    assert.match(pickers[0].text, /Page 1\/3/);
    assert.match(pickers[1].text, /Page 2\/3/);
    assert.ok(pickers.every(form => form.buttons.length <= 13));
    assert.match(pickers[2].text, /1 match\(es\)/);
});

test("empty search can be cleared without closing the toolbox", async () => {
    const f = await fixture();
    f.answers.push(choose("Player care"), choose("Heal player"), choose("Search players"),
        { canceled: false, formValues: ["missing"] }, choose("Clear search"), choose("Player Two"));
    f.command("menu"); await f.flush();
    assert.ok(f.forms.some(form => form.text?.includes("No matching players")));
    assert.equal(f.member.getComponent("minecraft:health").currentValue, 20);
});

test("world-menu cancellation is inert and confirmation applies once", async () => {
    const f = await fixture();
    f.answers.push(choose("World controls"), choose("Set night"), choose("Back"),
        choose("Set morning"), choose("Apply change"));
    f.command("menu"); await f.flush();
    assert.equal(f.world.time, 1000);
    assert.ok(!f.logs.some(line => line.includes("/admin:night")));
    assert.equal(f.logs.filter(line => line.includes("/admin:day")).length, 1);
    assert.match(f.forms.at(-1).text, /World time set to morning/);
});

test("travel preview shows destination and refuses a changed destination", async () => {
    const f = await fixture();
    f.answers.push(choose("Travel"), choose("Bring player"), choose("Player Two"), (_player, form) => {
        assert.match(form.text, /Coordinates: §f1, 64, 0/);
        f.admin.location.x = 10;
        return { canceled: false, selection: 1 };
    });
    f.command("menu"); await f.flush();
    assert.equal(f.member.teleports.length, 0);
    assert.match(f.admin.messages.join(), /destination changed/);
});

test("travel confirmation rechecks player availability and operator permissions", async () => {
    for (const change of [f => f.players.splice(1, 1), f => { f.admin.playerPermissionLevel = 1; }]) {
        const f = await fixture();
        f.answers.push(choose("Travel"), choose("Bring player"), choose("Player Two"), () => {
            change(f); return { canceled: false, selection: 1 };
        });
        f.command("menu"); await f.flush();
        assert.equal(f.member.teleports.length, 0);
    }
});

test("announcement validates, previews, retains edited draft and sends once", async () => {
    const f = await fixture();
    f.answers.push(choose("Announcement"), { canceled: false, formValues: [""] },
        { canceled: false, formValues: ["First draft"] }, choose("Edit draft"), (_player, form) => {
            assert.equal(form.field[2].defaultValue, "First draft");
            return { canceled: false, formValues: ["Final draft"] };
        }, choose("Send to everyone"));
    f.command("menu"); await f.flush();
    assert.equal(f.broadcasts.length, 1); assert.match(f.broadcasts[0], /Final draft/);
    assert.ok(f.forms.some(form => form.heading === "§aPreview announcement"));
});

test("announcement cancellation sends nothing and returns to toolbox", async () => {
    const f = await fixture();
    f.answers.push(choose("Announcement"), { canceled: false, formValues: ["Do not send"] }, choose("Cancel"));
    f.command("menu"); await f.flush();
    assert.equal(f.broadcasts.length, 0); assert.match(f.forms.at(-1).text, /Nothing sent/);
});

test("status awaits a fresh reply rather than presenting cached info as live", async () => {
    const f = await fixture();
    f.manager.payload = "14:00|14:00|2h";
    f.command("status"); await f.flush();
    assert.match(f.admin.messages.join(), /Manager replied to this check/);
    assert.match(f.admin.messages.join(), /14:00/);
    assert.equal(f.logs.filter(line => line === "[MGR]|sync|").length, 1);
});

test("offline manager blocks restart and cancel requests, including legacy entry", async () => {
    const f = await fixture(); f.manager.reply = false;
    // Old cached/live info alone cannot satisfy the next fresh-request check.
    f.event("mgrback:info", "06:00|06:00|1h", undefined, { sourceType: "Server", sourceEntity: undefined });
    f.answers.push(choose("Yes, restart")); f.command("restart"); await f.flush();
    f.system.currentTick += 20; f.command("cancelrestart"); await f.flush();
    f.event("mgr:restart"); await f.flush();
    assert.ok(!f.logs.includes("[MGR]|restart|")); assert.ok(!f.logs.includes("[MGR]|cancel|"));
    assert.match(f.admin.messages.join(), /No change was requested/);
    assert.equal(f.logs.filter(line => line === "[MGR]|sync|").length, 3);
});

test("malformed manager data is not persisted or counted as a reply", async () => {
    const f = await fixture(); f.manager.payload = "bad|missing";
    f.command("status"); await f.flush();
    assert.equal(f.props.size, 0);
    assert.match(f.admin.messages.join(), /No manager reply/);
    assert.match(f.admin.messages.join(), /Cached \/ not verified/);
});

test("simultaneous status requests share one manager sync", async () => {
    const f = await fixture(); f.member.playerPermissionLevel = 2;
    f.command("status"); f.command("status", undefined, f.member); await f.flush();
    assert.equal(f.logs.filter(line => line === "[MGR]|sync|").length, 1);
    assert.match(f.admin.messages.join(), /Manager replied/); assert.match(f.member.messages.join(), /Manager replied/);
});

test("manager reply cannot authorize a player whose op status was revoked while waiting", async () => {
    const f = await fixture();
    f.manager.onReply = () => { f.admin.playerPermissionLevel = 1; };
    f.answers.push(choose("Yes, restart")); f.command("restart"); await f.flush();
    assert.ok(!f.logs.includes("[MGR]|restart|"));
});

test("overlapping legacy forms are rejected rather than queued invisibly", async () => {
    const f = await fixture();
    let release;
    f.answers.push(() => new Promise(resolve => { release = resolve; }));
    f.event("mgr:menu"); await f.flush();
    f.event("mgr:mods"); await f.flush();
    assert.equal(f.forms.length, 1);
    assert.match(f.admin.messages.join(), /admin window is already open/);
    release({ canceled: true }); await f.flush();
    f.event("mgr:mods"); await f.flush(); assert.equal(f.forms.length, 2);
});
