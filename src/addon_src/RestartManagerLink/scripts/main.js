/*
 * Restart Manager Link  v1.8.0
 *
 * In-game control panel for server_manager.py.
 *
 *   /scriptevent mgr:menu       <- opens the GUI
 *   /scriptevent mgr:restart    restart with a 1 minute warning
 *   /scriptevent mgr:cancel     cancel a pending restart, or skip the next one
 *   /scriptevent mgr:next       next restart time in chat
 *   /scriptevent mgr:schedule 06:00,14:00,22:00
 *   /scriptevent mgr:help
 *
 * Talks to the Python manager by writing a marked line to the content log,
 * which the manager reads off the server's stdout. The manager talks back by
 * running "scriptevent mgrback:info ..." on the console.
 *
 * Stable APIs only - @minecraft/server and @minecraft/server-ui.
 * No Beta APIs experiment required.
 */

import { system, world, ScriptEventSource } from "@minecraft/server";
import { CATALOG } from "./catalog.js";
import { createAdminTools, isOperator } from "./admin.js";
import {
    ActionFormData,
    ModalFormData,
    MessageFormData,
    FormCancelationReason,
} from "@minecraft/server-ui";

const NS = "mgr";          // player -> manager
const NS_BACK = "mgrback"; // manager -> this script
const PROP_INFO = "mgr:lastinfo";

const TEXT_COMMANDS = ["restart", "cancel", "next", "schedule", "help",
                       "menu", "mods", "sync", "admin"];

const admin = createAdminTools({ tell, showForm, summary, refreshInfo, sendManager: relayAs,
    openRestartMenu: mainMenu, confirmRestart });
admin.register();

/* ------------------------------------------------------------------ */
/* state pushed to us by the manager                                   */
/* ------------------------------------------------------------------ */

const info = {
    schedule: "unknown",
    nextTime: "unknown",
    nextIn: "unknown",
};
let infoRevision = 0;
let lastInfoTick;
let refreshPending;
const openForms = new Map();
const managerRequests = new Set();

function loadInfo() {
    try {
        const saved = world.getDynamicProperty(PROP_INFO);
        if (typeof saved === "string" && saved.length) applyInfo(saved, false);
    } catch (e) {
        /* first run, or dynamic properties unavailable */
    }
}

function applyInfo(payload, persist = true) {
    const text = String(payload);
    const parts = text.split("|").map(part => part.trim());
    if (text.length > 600 || /[\r\n\x00]/.test(text) || parts.length !== 3 || parts.some(part => !part)) return;
    if (parts[0]) info.schedule = parts[0].trim();
    if (parts[1]) info.nextTime = parts[1].trim();
    if (parts[2]) info.nextIn = parts[2].trim();
    if (!persist) return;
    infoRevision++;
    lastInfoTick = system.currentTick;
    try {
        world.setDynamicProperty(PROP_INFO, String(payload));
    } catch (e) {
        /* not fatal - we just lose it across a reload */
    }
}

function refreshInfo() {
    // Share one bounded request across simultaneous status/restart checks.
    if (refreshPending) return refreshPending;
    const revision = infoRevision;
    refreshPending = (async () => {
        relay("sync", "");
        for (let i = 0; i < 20; i++) {
            await system.waitTicks(5);
            if (infoRevision > revision) return true;
        }
        return false;
    })().finally(() => { refreshPending = undefined; });
    return refreshPending;
}

/* ------------------------------------------------------------------ */
/* talking to the manager                                              */
/* ------------------------------------------------------------------ */

function relay(command, payload) {
    // console.warn lands in the content log, which the manager is reading
    console.warn(`[MGR]|${command}|${payload ?? ""}`);
}

function tell(player, text) {
    try {
        if (player && typeof player.sendMessage === "function") {
            player.sendMessage(text);
            return;
        }
    } catch (e) {
        /* A departed player's private feedback must never become a broadcast. */
    }
}

async function relayAs(player, command, payload = "") {
    if (!admin.requireOperator(player)) return false;
    const id = player.id;
    if (managerRequests.has(id)) {
        tell(player, "§7A manager request is already in progress. Please wait.");
        return false;
    }
    managerRequests.add(id);
    try {
        if (!await refreshInfo()) {
            tell(player, "§cThe Python manager did not reply within about five seconds. No change was requested. Start the server through Launcher.bat and check content logging.");
            return false;
        }
        if (!admin.requireOperator(player)) return false;
        if (command === "sync") return true;
        relay(command, payload);
        if (["restart", "cancel", "schedule"].includes(command)) {
            report(player.name, "command", `/scriptevent mgr:${command} ${payload}`);
        }
        return true;
    } catch (err) {
        tell(player, `§cManager request failed: ${err}`);
        return false;
    } finally {
        managerRequests.delete(id);
    }
}

/* ------------------------------------------------------------------ */
/* form helpers                                                        */
/* ------------------------------------------------------------------ */

/*
 * A form cannot open while the player still has the chat window up, and
 * show() reports that as UserBusy. Retry for a few seconds until the chat
 * closes, then give up rather than looping forever.
 */
async function showForm(player, form, attempts = 30) {
    const id = player.id;
    if (openForms.has(id)) {
        tell(player, "§7An admin window is already open or waiting for chat to close. Finish it first.");
        return undefined;
    }
    const token = {};
    openForms.set(id, token);
    try {
    for (let i = 0; i < attempts; i++) {
        if (!admin.requireOperator(player)) return undefined;
        const response = await form.show(player);
        if (!admin.requireOperator(player)) return undefined;
        if (
            response.canceled &&
            response.cancelationReason === FormCancelationReason.UserBusy
        ) {
            await system.waitTicks(10);
            continue;
        }
        return response;
    }
    tell(player, "§cCouldn't open the menu - close chat and try again.");
    return undefined;
    } finally {
        if (openForms.get(id) === token) openForms.delete(id);
    }
}

function summary() {
    const age = lastInfoTick === undefined ? "§eCached / not verified this session"
        : `§7Last reply: §f${Math.max(0, Math.floor((system.currentTick - lastInfoTick) / 20))}s ago`;
    return (
        `${age}\n` +
        `§7Schedule: §f${info.schedule}\n` +
        `§7Next restart: §f${info.nextTime} §7(reported remaining: §f${info.nextIn}§7)\n`
    );
}

async function mainMenu(player) {
    const form = new ActionFormData()
        .title("Server Restart Manager")
        .body(summary())
        .button("Restart Now\n§7one minute warning")
        .button("Cancel / Skip\n§7stop or skip a restart")
        .button("Change Schedule\n§7set the daily times")
        .button("Mod Control Center\n§7pack settings and info")
        .button("Refresh\n§7ask the manager for current info")
        .button("Admin Toolbox\n§7players, travel and world controls")
        .button("Close");

    const res = await showForm(player, form);
    if (!res || res.canceled) return;

    switch (res.selection) {
        case 0:
            await confirmRestart(player);
            break;
        case 1:
            await relayAs(player, "cancel");
            break;
        case 2:
            await scheduleForm(player);
            break;
        case 3:
            await modMenu(player);
            break;
        case 4:
            if (await relayAs(player, "sync")) await mainMenu(player);
            break;
        case 5:
            await admin.menu(player);
            break;
        default:
            break;
    }
}

async function confirmRestart(player) {
    const form = new MessageFormData()
        .title("Restart the server?")
        .body(
            "Players get a one minute warning, then the world saves and the " +
            "server restarts.\n\nIt should be back within about a minute."
        )
        .button2("Yes, restart")
        .button1("No, go back");

    const res = await showForm(player, form);
    if (!res || res.canceled) return;

    // button2 is selection 1
    if (res.selection === 1) {
        if (await relayAs(player, "restart")) {
            tell(player, "§aRestart request sent to the responding manager. Watch for its countdown announcement.");
            return true;
        }
    }
    return false;
}

async function scheduleForm(player) {
    // NOTE: textField's third argument changed shape between the 1.x and 2.x
    // server-ui APIs, so we don't pass one. The current value goes in the
    // label instead, which works on every version.
    const current = info.schedule === "unknown" ? "(unknown)" : info.schedule;
    const form = new ModalFormData()
        .title("Restart Schedule")
        .textField(
            `Daily restart times, 24-hour, comma separated.\n` +
            `Currently: ${current}\n` +
            `Example: 06:00,14:00,22:00`,
            current
        );

    const res = await showForm(player, form);
    if (!res || res.canceled) return;

    const value = String((res.formValues && res.formValues[0]) || "").trim();
    if (!value) {
        tell(player, "§cNo times entered - schedule unchanged.");
        return;
    }
    if (!/^\s*\d{1,2}:\d{2}(\s*,\s*\d{1,2}:\d{2})*\s*$/.test(value)) {
        tell(player, "§cThat doesn't look like a list of HH:MM times. Nothing changed.");
        return;
    }
    await relayAs(player, "schedule", value);
}

/* ------------------------------------------------------------------ */
/* mod control center                                                  */
/* ------------------------------------------------------------------ */

function catalogPacks() {
    return (CATALOG && Array.isArray(CATALOG.packs)) ? CATALOG.packs : [];
}

async function modMenu(player) {
    const packs = catalogPacks();

    if (packs.length === 0) {
        const empty = new ActionFormData()
            .title("Mod Control Center")
            .body(
                "§7No pack settings have been catalogued yet.\n\n" +
                "On the server, run:\n§f  python build_mod_menu.py\n\n" +
                "§7then restart. It scans your installed behavior packs for " +
                "function-based settings and fills this menu in."
            )
            .button("Back");
        const res = await showForm(player, empty);
        if (res && !res.canceled) await mainMenu(player);
        return;
    }

    const form = new ActionFormData()
        .title("Mod Control Center")
        .body(`§7${packs.length} pack(s) catalogued §8(${CATALOG.generated || "unknown date"})`);

    for (const pack of packs) {
        const n = (pack.toggles || []).length + (pack.configItems || []).length
                + (pack.functions || []).length;
        form.button(`${pack.name}\n§7v${pack.version} - ${n} option(s)`);
    }
    form.button("Back");

    const res = await showForm(player, form);
    if (!res || res.canceled) return;

    if (res.selection >= packs.length) {
        await mainMenu(player);
        return;
    }
    await packMenu(player, packs[res.selection]);
}

function hasTag(player, tag) {
    try {
        return player.hasTag(tag);
    } catch (e) {
        return false;
    }
}

async function packMenu(player, pack) {
    const toggles = pack.toggles || [];
    const items = pack.configItems || [];
    const funcs = pack.functions || [];

    let body = "";
    if (pack.desc) body += `§7${pack.desc}\n\n`;
    body += `§7Version: §f${pack.version}\n`;
    if (pack.subpacks && pack.subpacks.length) {
        body += `§7Variants: §f${pack.subpacks.join(", ")}\n`;
    }
    if (toggles.length) {
        body += "§8Toggles are per-player and apply to you only.\n";
    }
    if (pack.truncated) {
        body += "§8(function list truncated)\n";
    }

    const form = new ActionFormData().title(pack.name).body(body);

    // 1. per-player tag toggles, showing their real current state
    for (const t of toggles) {
        const on = hasTag(player, t.tag);
        const mark = on ? "§a[ON] " : "§8[OFF] ";
        form.button(t.desc ? `${mark}§r${t.label}\n§7${t.desc.slice(0, 40)}`
                           : `${mark}§r${t.label}`);
    }
    // 2. the pack's own config item, which may do more than flip a tag
    for (const item of items) {
        form.button(`Get the pack's config item\n§7${item}`);
    }
    // 3. functions the pack files under config/ or settings/
    for (const fn of funcs) {
        form.button(fn.desc ? `${fn.label}\n§7${fn.desc.slice(0, 40)}` : fn.label);
    }
    form.button("Back");

    const res = await showForm(player, form);
    if (!res || res.canceled) return;

    let i = res.selection;
    if (i < toggles.length) {
        await flipTag(player, pack, toggles[i]);
        return;
    }
    i -= toggles.length;
    if (i < items.length) {
        await giveItem(player, pack, items[i]);
        return;
    }
    i -= items.length;
    if (i < funcs.length) {
        await runFunction(player, pack, funcs[i]);
        return;
    }
    await modMenu(player);
}

async function flipTag(player, pack, toggle) {
    if (!admin.requireOperator(player)) return;
    const on = hasTag(player, toggle.tag);
    try {
        if (on) player.removeTag(toggle.tag);
        else player.addTag(toggle.tag);
        tell(player, `§a${toggle.label} is now ${on ? "§cOFF" : "§aON"}`);
        if (on && pack.configItems && pack.configItems.length) {
            // some packs do extra cleanup when switching a setting off
            tell(player, "§7If this doesn't fully take effect, use the pack's own "
                       + "config item - it may do extra cleanup.");
        }
    } catch (err) {
        tell(player, `§cCouldn't change that setting: ${err}`);
    }
    await packMenu(player, pack);
}

async function giveItem(player, pack, itemId) {
    if (!admin.requireOperator(player)) return;
    try {
        player.runCommand(`give @s ${itemId}`);
        tell(player, `§aGave you ${itemId} - use it to open ${pack.name}'s own settings.`);
    } catch (err) {
        tell(player, `§cCouldn't give that item: ${err}`);
    }
}

async function runFunction(player, pack, fn) {
    const confirm = new MessageFormData()
        .title(fn.label)
        .body(
            (fn.desc ? `§7${fn.desc}\n\n` : "") +
            `§7This runs:\n§f/function ${fn.cmd}\n\n` +
            "§7Pack settings usually take effect immediately, but some need a " +
            "world reload."
        )
        .button2("Run it")
        .button1("Back");

    const res = await showForm(player, confirm);
    if (!res || res.canceled) return;

    if (res.selection !== 1) {
        await packMenu(player, pack);
        return;
    }

    if (!admin.requireOperator(player)) return;
    try {
        // runCommand from a script runs with full permission
        player.runCommand(`function ${fn.cmd}`);
        tell(player, `§a[${pack.name}] ran /function ${fn.cmd}`);
    } catch (err) {
        tell(player, `§cFailed to run /function ${fn.cmd}: ${err}`);
    }
}


/* ------------------------------------------------------------------ */
/* activity reporting                                                  */
/*                                                                     */
/* The Bedrock console logs connects and disconnects and nothing else. */
/* Deaths and chat only exist inside the scripting API, so we forward  */
/* them to the launcher over the same content-log channel.             */
/* ------------------------------------------------------------------ */

function report(player, kind, detail) {
    if (!player || reportingOff) return;
    try {
        console.warn("[MGR]|ev|" + JSON.stringify({
            p: String(player).slice(0, 40),
            k: kind,
            d: String(detail ?? "").slice(0, 300),
        }));
    } catch (e) {
        /* never let reporting break gameplay */
    }
}

/*
 * Every handler below runs inside safe(). An unhandled exception in a script
 * event trips Bedrock's watchdog, whose default action is to shut the world
 * down - which players experience as being disconnected. Reporting is a
 * convenience; it must never be able to take the server with it.
 */
let reportFailures = 0;
let reportingOff = false;

function safe(label, fn) {
    return (event) => {
        if (reportingOff) return;
        try {
            fn(event);
        } catch (err) {
            reportFailures++;
            if (reportFailures <= 3) {
                console.warn(`[MGR]|ready|activity handler '${label}' failed: ${err}`);
            }
            if (reportFailures >= 10) {
                reportingOff = true;
                console.warn("[MGR]|ready|activity reporting switched off after "
                           + "repeated errors - the server is unaffected");
            }
        }
    };
}

/* Attach an event handler if this server build has that event at all.
   Returns true when it took. */
function attach(path, handler) {
    try {
        const parts = path.split(".");
        let node = world;
        for (const part of parts) node = node[part];
        node.subscribe(handler);
        return true;
    } catch (e) {
        return false;
    }
}

function describeDeath(event) {
    let cause = "died";
    try {
        if (event.damageSource && event.damageSource.cause) {
            cause = String(event.damageSource.cause);
        }
    } catch (e) { /* ignore */ }
    try {
        const killer = event.damageSource && event.damageSource.damagingEntity;
        if (killer) {
            const id = String(killer.typeId || "");
            const who = id === "minecraft:player"
                ? String(killer.name || "another player")
                : id.replace("minecraft:", "");
            if (who) return `${cause} by ${who}`;
        }
    } catch (e) { /* the killer may already be gone - the cause alone is fine */ }
    return cause;
}

function startReporting() {
    const wired = [];

    if (attach("afterEvents.playerSpawn", safe("join", (e) => {
        if (e.initialSpawn && e.player) report(e.player.name, "join", "spawned");
    }))) wired.push("join");

    if (attach("afterEvents.playerLeave", safe("leave", (e) => {
        if (e && e.playerName) report(e.playerName, "leave", "");
    }))) wired.push("leave");

    if (attach("afterEvents.entityDie", safe("death", (e) => {
        // reading properties of a just-removed entity can throw, hence safe()
        const dead = e && e.deadEntity;
        if (!dead) return;
        let isPlayer = false;
        try {
            isPlayer = dead.typeId === "minecraft:player";
        } catch (err) {
            return;
        }
        if (!isPlayer) return;
        let name = "";
        try {
            name = dead.name;
        } catch (err) {
            return;
        }
        if (name) report(name, "death", describeDeath(e));
    }))) wired.push("death");

    /*
     * Chat: only the AFTER event is safe to touch. The before-event runs in a
     * restricted phase where a mistake cancels the message or worse, so if the
     * after-event is unavailable on this build we simply do not record chat.
     */
    if (attach("afterEvents.chatSend", safe("chat", (e) => {
        const name = (e.sender && e.sender.name) || e.playerName;
        if (name && e.message) report(name, "chat", e.message);
    }))) wired.push("chat");

    console.warn(`[MGR]|ready|activity reporting: ${wired.join(", ") || "none"}`);
}

/* ------------------------------------------------------------------ */
/* command entry point                                                 */
/* ------------------------------------------------------------------ */

system.afterEvents.scriptEventReceive.subscribe(
    (event) => {
        const id = String(event.id || "").toLowerCase();

        // manager -> script
        if (id === `${NS_BACK}:info`) {
            if (event.sourceType !== ScriptEventSource.Server || event.sourceEntity || event.sourceBlock) return;
            applyInfo(event.message || "");
            return;
        }

        if (!id.startsWith(`${NS}:`)) return;

        const command = id.slice(NS.length + 1);
        const payload = String(event.message || "").trim();
        const player = event.sourceEntity;

        if (event.sourceType !== ScriptEventSource.Entity || !isOperator(player) || event.initiator || event.sourceBlock) {
            tell(player, "§cOnly an in-game server operator can use the manager tools.");
            return;
        }

        if (!TEXT_COMMANDS.includes(command)) {
            tell(player, `§cUnknown command "${command}". Try /scriptevent mgr:help`);
            return;
        }

        if (command === "menu" || command === "mods" || command === "admin") {
            const opener = command === "mods" ? modMenu : command === "admin" ? admin.menu : mainMenu;
            if (!player) {
                console.warn("[MGR-LINK] this command must be run by a player");
                return;
            }
            // defer a tick: forms cannot open from inside the event callback
            system.run(() => {
                opener(player).catch((err) => {
                    tell(player, `§cMenu error: ${err}`);
                });
            });
            return;
        }

        if (command === "schedule" && payload.length === 0) {
            tell(player, "§cUsage: /scriptevent mgr:schedule 06:00,14:00,22:00");
            return;
        }

        if (command === "help") {
            admin.execute(player, "help", payload);
            return;
        }
        if (payload.length > 200 || /[\r\n\x00]/.test(payload)) {
            tell(player, "§cCommand payload is too long or contains invalid characters.");
            return;
        }
        relayAs(player, command, payload).then(sent => {
            if (sent) tell(player, "§7[restart manager] request sent to the responding manager.");
        });
    },
    { namespaces: [NS, NS_BACK] }
);

/*
 * worldLoad exists on the 2.x API; older lines called it worldInitialize.
 * Try both, and fall back to a plain delayed sync so a missing event can
 * never stop the rest of the script from loading.
 */
function onWorldReady(handler) {
    const candidates = [
        () => world.afterEvents.worldLoad.subscribe(handler),
        () => world.afterEvents.worldInitialize.subscribe(handler),
    ];
    for (const attach of candidates) {
        try {
            attach();
            return true;
        } catch (e) {
            /* try the next one */
        }
    }
    try {
        system.runTimeout(handler, 100); // ~5 seconds after load
        return true;
    } catch (e) {
        return false;
    }
}

onWorldReady(() => {
    loadInfo();
    relay("sync", "");
    startReporting();
});

console.warn("[MGR]|ready|restart manager link loaded");
