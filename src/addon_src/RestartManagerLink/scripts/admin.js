/* Operator-only native commands. All world changes run outside the command callback. */
import {
    system, world, CommandPermissionLevel, PlayerPermissionLevel,
    CustomCommandParamType, CustomCommandStatus, CustomCommandSource,
} from "@minecraft/server";
import { ActionFormData, ModalFormData, MessageFormData } from "@minecraft/server-ui";
import { ADMIN_COMMANDS as COMMANDS } from "./reference.js";
import { createHelp } from "./help.js";

const BACK = "mgr:admin_return";
const PREFIX = "§a[Admin]§r ";
const TARGET = { name: "player", type: CustomCommandParamType.PlayerSelector };
const READ_ONLY = new Set(["help", "menu", "status", "inspect"]);
const PAGE_SIZE = 8;
const GROUPS = [
    { title: "Player care", subtitle: "Health, hunger & inspection", actions: [
        ["heal", "Heal player", true], ["feed", "Feed player", true], ["inspect", "Inspect player", true]] },
    { title: "Travel", subtitle: "Visit, bring & return players", actions: [
        ["goto", "Go to player", true], ["bring", "Bring player", true],
        ["back", "My return point"], ["return", "Return player", true]] },
    { title: "World controls", subtitle: "Morning, night & clear skies", actions: [
        ["day", "Set morning"], ["night", "Set night"], ["clearweather", "Clear skies · 10 minutes"]] },
];

export function isOperator(player) {
    try {
        return player?.typeId === "minecraft:player" && player.isValid !== false
            && player.playerPermissionLevel === PlayerPermissionLevel.Operator;
    } catch (_) { return false; }
}

export function cleanText(text) {
    return String(text ?? "").replace(/§./g, "").replace(/[\x00-\x1f\x7f]/g, " ").trim();
}

function online(player) {
    const found = world.getAllPlayers().find(p => p.id === player?.id);
    if (!found) throw new Error("That player is no longer online. Reopen the menu or try again.");
    return found;
}

function oneTarget(actor, value, required = false) {
    if (value === undefined && !required) return online(actor);
    if (!Array.isArray(value) || value.length !== 1) {
        throw new Error("Choose exactly one online player. Broad selectors such as @a are rejected when they match multiple players.");
    }
    return online(value[0]);
}

function locationOf(player) {
    return { dimension: player.dimension.id, location: { ...player.location }, rotation: player.getRotation() };
}

function savedLocation(player) {
    const raw = player.getDynamicProperty(BACK);
    if (typeof raw !== "string") throw new Error("No saved return point. Use admin:goto or admin:bring first.");
    let data;
    try { data = JSON.parse(raw); } catch (_) { throw new Error("The saved return point is invalid."); }
    if (!data || typeof data.dimension !== "string" ||
        ![data.location?.x, data.location?.y, data.location?.z, data.rotation?.x, data.rotation?.y].every(Number.isFinite)) {
        throw new Error("The saved return point is invalid.");
    }
    return data;
}

function movePlayer(player, destination) {
    const previous = player.getDynamicProperty(BACK);
    const from = locationOf(player);
    // Saving first means a storage error cannot strand someone without a return point.
    player.setDynamicProperty(BACK, JSON.stringify(from));
    try {
        if (!player.tryTeleport(destination.location, {
            dimension: world.getDimension(destination.dimension), rotation: destination.rotation,
            checkForBlocks: true, keepVelocity: false,
        })) throw new Error("Teleport blocked or destination not loaded. Your return point was kept; move closer and try again.");
    } catch (err) {
        player.setDynamicProperty(BACK, previous);
        throw err;
    }
    return `${cleanText(player.name)}: ${from.dimension} ${coords(from.location)} -> ${destination.dimension} ${coords(destination.location)}`;
}

function coords(location) {
    return [location.x, location.y, location.z].map(Math.floor).join(", ");
}

function attribute(player, id) {
    const component = player.getComponent(id);
    if (!component) throw new Error(`This player does not expose ${id}. Nothing changed.`);
    return component;
}

export function createAdminTools({ tell, showForm, summary, refreshInfo, sendManager, openRestartMenu, confirmRestart }) {
    const recent = new Map();
    const menus = new Map();
    const pending = new Map();
    const say = (player, message) => tell(player, PREFIX + message);

    function audit(player, command, detail) {
        // Existing player-history "command" category; JSON escaping keeps the bridge one line.
        try {
            console.warn("[MGR]|ev|" + JSON.stringify({ p: player.name, k: "command",
                d: cleanText(`/admin:${command} ${detail || ""}`).slice(0, 300) }));
        } catch (_) { /* Invalid player handles must not turn logging into an unhandled rejection. */ }
    }

    function requireOperator(player) {
        if (isOperator(player)) return true;
        say(player, "§cOnly an online server operator can use this tool.");
        return false;
    }

    const { browse: help } = createHelp({ showForm, tell, requireOperator });

    async function handle(player, command, value, expectedDestination) {
        if (!requireOperator(player)) return "Permission lost. Nothing changed.";
        let detail = "";
        if (command === "help") {
            await help(player, value);
            return;
        }
        if (command === "menu") { await menu(player); return; }
        if (command === "status") {
            const replied = await refreshInfo();
            if (!requireOperator(player)) return "Permission lost.";
            detail = `§f${world.getAllPlayers().length} player(s) online\n` +
                (replied ? "§aManager replied to this check.\n" : "§eNo manager reply in about five seconds. Start through Launcher.bat and check content logging.\n") + summary();
            say(player, detail);
            return detail;
        }
        if (["heal", "feed", "inspect", "goto", "bring", "return"].includes(command)) {
            const target = oneTarget(player, value, ["goto", "bring", "return"].includes(command));
            detail = cleanText(target.name);
            if (command === "inspect") {
                const health = attribute(target, "minecraft:health");
                let saved = "No saved return point";
                try { const point = savedLocation(target); saved = `${point.dimension} · ${coords(point.location)}`; } catch (_) { /* optional metadata */ }
                detail = `§f${detail}\n§7Health: §f${Math.round(health.currentValue * 10) / 10}/${health.effectiveMax}\n§7Mode: §f${target.getGameMode()}\n§7Dimension: §f${target.dimension.id}\n§7Position: §f${coords(target.location)}\n§7Return point: §f${saved}`;
                say(player, detail);
                return detail;
            }
            if (command === "heal") attribute(target, "minecraft:health").resetToMaxValue();
            if (command === "feed") {
                // Fetch everything before mutating to avoid a missing-component partial refill.
                const hunger = attribute(target, "minecraft:player.hunger");
                const saturation = attribute(target, "minecraft:player.saturation");
                const exhaustion = attribute(target, "minecraft:player.exhaustion");
                hunger.resetToMaxValue(); saturation.resetToMaxValue(); exhaustion.resetToMinValue();
            }
            if (command === "goto" || command === "bring") {
                if (target.id === player.id) throw new Error("Choose a different player.");
                const destination = command === "goto" ? locationOf(target) : locationOf(player);
                checkDestination(expectedDestination, destination);
                detail = command === "goto" ? `to ${cleanText(target.name)}; ${movePlayer(player, destination)}` : movePlayer(target, destination);
            }
            if (command === "return") {
                const destination = savedLocation(target);
                checkDestination(expectedDestination, destination);
                detail = movePlayer(target, destination);
            }
            if (target.id !== player.id) say(target, `§7${cleanText(player.name)} used §f/admin:${command}§7 with you.`);
        } else if (command === "back") {
            const destination = savedLocation(player);
            checkDestination(expectedDestination, destination);
            detail = movePlayer(player, destination);
        } else if (command === "day" || command === "night") {
            world.setTimeOfDay(command === "day" ? 1000 : 13000);
            detail = command === "day" ? "World time set to morning." : "World time set to night.";
        } else if (command === "clearweather") {
            world.getDimension("overworld").runCommand("weather clear 600");
            detail = "Overworld weather cleared for ten minutes.";
        } else if (command === "announce") {
            const message = cleanText(value);
            if (!message || message.length > 180) throw new Error('Use /admin:announce "your message" (1–180 characters).');
            // Never interpolate announcement text into a Minecraft command.
            world.sendMessage(`§6[Announcement] §f${message} §8— ${cleanText(player.name)}`);
            detail = message;
        } else if (command === "restart") {
            return await confirmRestart(player) ? "Restart requested. Watch for the countdown." : "No restart request sent.";
        } else if (command === "cancelrestart") {
            if (!await sendManager(player, "cancel")) return "No cancel / skip request sent.";
            detail = "Cancel / skip request sent to the responding Python manager.";
            say(player, detail);
            return detail; // Shared manager path audits this request once.
        } else throw new Error("Unknown admin command. Use /admin:help.");
        audit(player, command, detail);
        say(player, `§a${command}: §f${detail}`);
        return detail;
    }

    function checkDestination(expected, actual) {
        if (expected && (expected.dimension !== actual.dimension ||
            ["x", "y", "z"].some(axis => Math.abs(expected.location[axis] - actual.location[axis]) > 1))) {
            throw new Error("The destination changed after the preview. Review the move again; nothing was teleported.");
        }
    }

    function cooldown(player, command) {
        if (READ_ONLY.has(command)) return "";
        if (system.currentTick - (recent.get(player.id) ?? -100) < 20) return "Please wait one second between admin actions.";
        recent.set(player.id, system.currentTick);
        return "";
    }

    async function perform(player, command, value, destination) {
        try {
            player = online(player);
            if (!requireOperator(player)) return "Permission lost. Nothing changed.";
            const warning = cooldown(player, command);
            if (warning) { say(player, `§e${warning}`); return warning; }
            return await handle(player, command, value, destination);
        } catch (err) {
            const message = cleanText(err.message || err);
            say(player, `§c${message}`);
            audit(player, command, `FAILED: ${message}`);
            return `§c${message}`;
        }
    }

    function execute(player, command, value) {
        if (!isOperator(player)) return { status: CustomCommandStatus.Failure, message: "This command is for in-game server operators only." };
        const id = player.id;
        if (pending.has(id) || menus.has(id)) {
            return { status: CustomCommandStatus.Failure, message: "An admin action or menu is already open. Finish it before starting another." };
        }
        if (!READ_ONLY.has(command) && system.currentTick - (recent.get(id) ?? -100) < 20) {
            return { status: CustomCommandStatus.Failure, message: "Please wait one second between admin actions." };
        }
        const token = {};
        pending.set(id, token);
        system.run(() => {
            // Re-resolve after deferring: leaving the server or losing operator privileges cancels the action.
            perform(player, command, value).finally(() => {
                if (pending.get(id) === token) pending.delete(id);
            });
        });
        return { status: CustomCommandStatus.Success };
    }

    async function choosePlayer(player, command) {
        let page = 0, query = "";
        while (requireOperator(player)) {
            // Stable IDs, not display text, are carried through to the action.
            const matches = world.getAllPlayers().filter(p => !["goto", "bring"].includes(command) || p.id !== player.id)
                .filter(p => cleanText(p.name).toLowerCase().includes(query.toLowerCase()))
                .sort((a, b) => a.id === player.id ? -1 : b.id === player.id ? 1 : a.name.localeCompare(b.name));
            const pages = Math.max(1, Math.ceil(matches.length / PAGE_SIZE));
            page = Math.min(page, pages - 1);
            const choices = matches.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map(p => ({
                target: p,
                label: `${cleanText(p.name)}${p.id === player.id ? " §a(you)" : ""}\n§7${p.dimension.id.replace("minecraft:", "")} · ${coords(p.location)}`,
            }));
            choices.push({ action: "search", label: "Search players" });
            if (query) choices.push({ action: "clear", label: "Clear search" });
            if (page > 0) choices.push({ action: "previous", label: "Previous page" });
            if (page + 1 < pages) choices.push({ action: "next", label: "Next page" });
            choices.push({ action: "refresh", label: "Refresh online players" }, { action: "back", label: "Back" });
            const form = new ActionFormData().title(`§aAdmin · ${command}`)
                .body(`§f${matches.length} match(es) §8· Page ${page + 1}/${pages}\n` +
                    (query ? `§7Search: §f${query}\n` : "") +
                    (matches.length ? "§7Choose one online player." : "§eNo matching players. Try a different search or refresh."));
            for (const choice of choices) form.button(choice.label);
            const result = await showForm(player, form);
            if (!result || result.canceled) return undefined;
            const choice = choices[result.selection];
            if (!choice) return undefined;
            if (choice.target) return choice.target;
            if (choice.action === "back") return null;
            if (choice.action === "clear") { query = ""; page = 0; }
            if (choice.action === "next") page++;
            if (choice.action === "previous") page--;
            if (choice.action === "search") {
                const response = await showForm(player, new ModalFormData().title("§aFind player")
                    .textField("Part of an online gamertag (up to 40 characters)", "Gamertag", { defaultValue: query }));
                if (!response || response.canceled) return undefined;
                query = cleanText(response.formValues?.[0]).slice(0, 40);
                page = 0;
            }
        }
        return undefined;
    }

    async function confirm(player, title, body, accept = "Confirm") {
        const response = await showForm(player, new MessageFormData().title(`§a${title}`)
            .body(body).button1("Back").button2(accept));
        return !response || response.canceled ? undefined : response.selection === 1;
    }

    async function menuAction(player, command, target) {
        const value = target ? [target] : undefined;
        let destination;
        try {
            if (["goto", "bring", "return", "back"].includes(command)) {
                const current = target ? online(target) : online(player);
                destination = command === "goto" ? locationOf(current) : command === "bring" ? locationOf(player) : savedLocation(current);
                const moved = command === "goto" || command === "back" ? player : current;
                const accepted = await confirm(player, "Review teleport",
                    `§fMove: ${cleanText(moved.name)}\n§7Destination: §f${destination.dimension}\n§7Coordinates: §f${coords(destination.location)}\n\n` +
                    "§7The previous location becomes the return point. Block collisions are checked; drops, liquids and other hazards are still your responsibility.", "Teleport");
                if (accepted === undefined) return { closed: true };
                if (!accepted) return { notice: "Move cancelled. Nothing changed." };
            }
            if (["day", "night", "clearweather"].includes(command)) {
                const accepted = await confirm(player, "Review world change",
                    `§f/admin:${command}\n\n§7This changes ${command === "clearweather" ? "Overworld weather for ten minutes" : "the world's time of day"} for everyone.`, "Apply change");
                if (accepted === undefined) return { closed: true };
                if (!accepted) return { notice: "World change cancelled." };
            }
            const notice = await perform(player, command, value, destination);
            if (command === "inspect") {
                const response = await showForm(player, new ActionFormData().title("§aPlayer inspection").body(notice).button("Back"));
                if (!response || response.canceled) return { closed: true };
            }
            return { notice };
        } catch (err) {
            const notice = `§c${cleanText(err.message || err)}`;
            say(player, notice);
            return { notice };
        }
    }

    async function announcement(player) {
        let draft = "", error = "";
        while (requireOperator(player)) {
            const response = await showForm(player, new ModalFormData().title("§aServer announcement")
                .textField(`${error ? error + "\n" : ""}Message to every online player (1–180 characters)`,
                    "Please meet at spawn.", { defaultValue: draft }));
            if (!response || response.canceled) return { closed: true };
            draft = cleanText(response.formValues?.[0]);
            if (!draft || draft.length > 180) { error = "§cEnter 1–180 characters.§r"; continue; }
            const preview = new ActionFormData().title("§aPreview announcement")
                .body(`§6[Announcement] §f${draft} §8— ${cleanText(player.name)}\n\n§7${draft.length}/180 characters · ${world.getAllPlayers().length} players online`)
                .button("Send to everyone").button("Edit draft").button("Cancel");
            const result = await showForm(player, preview);
            if (!result || result.canceled) return { closed: true };
            if (result.selection === 0) return { notice: await perform(player, "announce", draft) };
            if (result.selection === 2) return { notice: "Announcement cancelled. Nothing sent." };
            if (result.selection !== 1) return { closed: true };
            error = "";
        }
        return { closed: true };
    }

    async function menu(player) {
        if (!requireOperator(player)) return;
        if (menus.has(player.id)) { say(player, "§7Your toolbox is already open. Close chat to see it."); return; }
        const id = player.id, token = {};
        menus.set(id, token);
        let group, notice = "";
        try {
            while (requireOperator(player)) {
                const actions = group ? [...group.actions, ["back", "Back to toolbox"]] : [
                    ...GROUPS.map((item, i) => [`group${i}`, `${item.title}\n§7${item.subtitle}`]),
                    ["announce", "Announcement\n§7write, preview & send"],
                    ["status", "Server status\n§7check the manager connection"],
                    ["manager", "Restart & mod controls\n§7open the manager"],
                    ["help", "Command reference\n§7core, admin, manager & mod help"], ["close", "Close"],
                ];
                const form = new ActionFormData().title(group ? `§aAdmin · ${group.title}` : "§aADMIN TOOLBOX")
                    .body(`§f${cleanText(player.name)} §8/ §aOperator\n§7${world.getAllPlayers().length} online · Single-player targeting · Logged actions\n` +
                        (notice ? `\n§r${notice}\n` : ""));
                for (const [, label] of actions) form.button(label);
                const result = await showForm(player, form);
                if (!result || result.canceled || !actions[result.selection]) return;
                const [command, , select] = actions[result.selection];
                if (group && result.selection === actions.length - 1) { group = undefined; notice = ""; continue; }
                if (command === "close") return;
                if (command.startsWith("group")) { group = GROUPS[Number(command.slice(5))]; notice = ""; continue; }
                if (command === "manager") {
                    menus.delete(id); // The manager can navigate back to a new toolbox session.
                    await openRestartMenu(player);
                    return;
                }
                if (command === "announce") {
                    const outcome = await announcement(player);
                    if (outcome.closed) return;
                    notice = outcome.notice;
                    continue;
                }
                let target;
                if (select) {
                    target = await choosePlayer(player, command);
                    if (target === undefined) return;
                    if (target === null) continue;
                }
                const outcome = await menuAction(player, command, target);
                if (outcome.closed) return;
                notice = outcome.notice;
            }
        } finally { if (menus.get(id) === token) menus.delete(id); }
    }

    function register() {
        system.beforeEvents.startup.subscribe(({ customCommandRegistry }) => {
            for (const [name, description, kind] of COMMANDS) {
                const definition = { name: `admin:${name}`, description,
                    permissionLevel: CommandPermissionLevel.Admin,
                    // Operator gate is independent of the world's cheats toggle; no settings are changed.
                    cheatsRequired: false };
                if (kind === "optional") definition.optionalParameters = [TARGET];
                if (kind === "query") definition.optionalParameters = [{ name: "search", type: CustomCommandParamType.String }];
                if (kind === "required") definition.mandatoryParameters = [TARGET];
                if (kind === "message") definition.mandatoryParameters = [{ name: "message", type: CustomCommandParamType.String }];
                customCommandRegistry.registerCommand(definition, (origin, value) => {
                    if (origin.sourceType !== CustomCommandSource.Entity || origin.sourceBlock || origin.initiator) {
                        return { status: CustomCommandStatus.Failure, message: "Use this command directly in game as an operator, not from the console, an NPC or a command block." };
                    }
                    return execute(origin.sourceEntity, name, value);
                });
            }
            console.warn(`[ADMIN] Registered ${COMMANDS.length} operator commands. Open /admin:menu`);
        });
        world.afterEvents.playerLeave.subscribe(({ playerId }) => { recent.delete(playerId); menus.delete(playerId); pending.delete(playerId); });
    }

    return { register, menu, execute, requireOperator, help };
}
