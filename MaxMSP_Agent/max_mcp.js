
autowatch = 1; // 1
inlets = 1; // Receive network messages here
outlets = 3; // For status, responses, etc.

// Subpatcher navigation state
var root_patcher = this.patcher;
var current_patcher = this.patcher;
var patcher_stack = [];  // Stack of {patcher, name} for navigation history
var base_context_depth = 0; // hidden baseline depth when a managed workspace is active
var active_workspace_target = "host";
var active_workspace_varname = "";

// Legacy alias - some functions still use 'p'
var p = this.patcher;

var obj_count = 0;
var boxes = [];
var lines = [];

// Preflight check: require get_avoid_rect_position before placing objects
// Resets when entering/exiting subpatchers (new context = new layout)
var avoid_rect_called = false;

// Large patch warning
var objects_added_counter = 0;
var OBJECT_COUNT_CHECK_INTERVAL = 10;
var LARGE_PATCH_THRESHOLD = 80;

// Signal safety auto-check (triggers every N MSP objects created)
var msp_objects_counter = 0;
var MSP_SAFETY_CHECK_INTERVAL = 10;

function effective_depth() {
    var depth = patcher_stack.length - base_context_depth;
    return depth < 0 ? 0 : depth;
}

function effective_path() {
    var path = [];
    for (var i = base_context_depth; i < patcher_stack.length; i++) {
        path.push(patcher_stack[i].name);
    }
    return path;
}

function safe_parse_json(str) {
    try {
        return JSON.parse(str);
    } catch (e) {
        outlet(0, "error", "Invalid JSON: " + e.message);
        return null;
    }
}

function split_long_string(inString, maxLength) {
    // var longString = inString.replace(/\s+/g, "");
    var result = [];
    for (var i = 0; i < inString.length; i += maxLength) {
        result.push(inString.substring(i, i + maxLength));
    }
    return result;
}

function count_root_patcher_objects() {
    var count = 0;
    // Use apply (not applydeep) to only count objects in root patcher
    root_patcher.apply(function(obj) {
        // Skip patchlines and internal objects
        if (obj.maxclass && obj.maxclass !== "patchline") {
            count++;
        }
    });
    return count;
}

function check_large_patch_warning() {
    objects_added_counter++;
    if (objects_added_counter >= OBJECT_COUNT_CHECK_INTERVAL) {
        objects_added_counter = 0;
        var count = count_root_patcher_objects();
        if (count > LARGE_PATCH_THRESHOLD) {
            return "WARNING: Large patch (" + count + " objects in root patcher). Consider using encapsulate() to organize into subpatchers.";
        }
    }
    return null;
}

function emit_response_envelope(request_id, state, results, error, meta) {
    var envelope = {
        protocol_version: "2.0",
        request_id: request_id || null,
        state: state,
        timestamp_ms: Date.now()
    };
    if (state === "failed") {
        envelope.error = error || {
            code: "INTERNAL_ERROR",
            message: "Unknown failure in Max bridge",
            recoverable: false,
            details: {}
        };
    } else {
        envelope.results = results;
    }
    if (meta) {
        envelope.meta = meta;
    }
    outlet(1, "response", JSON.stringify(envelope));
}

function respond_success(request_id, results, meta) {
    if (!request_id) return;
    emit_response_envelope(request_id, "succeeded", results, null, meta);
}

function respond_error(request_id, code, message, hint, recoverable, details) {
    if (!request_id) {
        outlet(0, "error", message);
        return;
    }
    var err = {
        code: code || "INTERNAL_ERROR",
        message: message || "Unknown bridge error",
        recoverable: (recoverable === undefined) ? true : !!recoverable,
        details: details || {}
    };
    if (hint) {
        err.hint = hint;
    }
    emit_response_envelope(request_id, "failed", null, err, null);
}

// Called when a message arrives at inlet 0 (from [udpreceive] or similar)
function anything() {
    var msg = arrayfromargs(messagename, arguments).join(" ");
    var data = safe_parse_json(msg);
    if (!data) return;

    // Support protocol envelopes while preserving legacy flat payload behavior.
    if (data.payload && typeof data.payload === "object") {
        for (var key in data.payload) {
            if (!(key in data)) {
                data[key] = data.payload[key];
            }
        }
    }

    switch (data.action) {
        case "fetch_test":
            if (data.request_id) {
                get_objects_in_patch(data.request_id);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id for fetch_test");
            }
            break;
        case "get_objects_in_patch":
            if (data.request_id) {
                get_objects_in_patch(data.request_id);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id for get_objects_in_patch");
            }
            break;
        case "get_objects_in_selected":
            if (data.request_id) {
                get_objects_in_selected(data.request_id);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id for get_objects_in_selected");
            }
            break;
        case "get_object_attributes":
            if (data.request_id && data.varname) {
                get_object_attributes(data.request_id, data.varname);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id or varname for get_object_attributes");
            }
            break;
        case "get_avoid_rect_position":
            if (data.request_id) {
                get_avoid_rect_position(data.request_id);
            }
            break;
        case "add_object":
            if (data.obj_type && data.position && data.varname && data.request_id) {
                add_object(data.position[0], data.position[1], data.obj_type, data.args, data.varname, data.request_id);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing obj_type, position, varname, or request_id for add_object");
            }
            break;
        case "remove_object":
            if (data.varname && data.request_id) {
                var rm = remove_object(data.varname);
                if (rm.success) {
                    respond_success(data.request_id, rm);
                } else {
                    respond_error(data.request_id, rm.error.code, rm.error.message, rm.error.hint, rm.error.recoverable, rm.error.details);
                }
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing varname or request_id for remove_object");
            }
            break;
        case "connect_objects":
            if (data.src_varname && data.dst_varname && data.request_id) {
                var con = connect_objects(data.src_varname, data.outlet_idx || 0, data.dst_varname, data.inlet_idx || 0);
                if (con.success) {
                    respond_success(data.request_id, con);
                } else {
                    respond_error(data.request_id, con.error.code, con.error.message, con.error.hint, con.error.recoverable, con.error.details);
                }
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing src_varname, dst_varname, or request_id for connect_objects");
            }
            break;
        case "disconnect_objects":
            if (data.src_varname && data.dst_varname && data.request_id) {
                var dis = disconnect_objects(data.src_varname, data.outlet_idx || 0, data.dst_varname, data.inlet_idx || 0);
                if (dis.success) {
                    respond_success(data.request_id, dis);
                } else {
                    respond_error(data.request_id, dis.error.code, dis.error.message, dis.error.hint, dis.error.recoverable, dis.error.details);
                }
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing src_varname, dst_varname, or request_id for disconnect_objects");
            }
            break;
        case "set_object_attribute":
            if (data.varname && data.attr_name && data.attr_value && data.request_id) {
                var attr = set_object_attribute(data.varname, data.attr_name, data.attr_value);
                if (attr.success) {
                    respond_success(data.request_id, attr);
                } else {
                    respond_error(data.request_id, attr.error.code, attr.error.message, attr.error.hint, attr.error.recoverable, attr.error.details);
                }
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing varname, attr_name, attr_value, or request_id");
            }
            break;
        case "set_message_text":
            if (data.varname && data.new_text && data.request_id) {
                var msg = set_message_text(data.varname, data.new_text);
                if (msg.success) {
                    respond_success(data.request_id, msg);
                } else {
                    respond_error(data.request_id, msg.error.code, msg.error.message, msg.error.hint, msg.error.recoverable, msg.error.details);
                }
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing varname, new_text, or request_id for set_message_text");
            }
            break;
        case "send_message_to_object":
            if (data.varname && data.message && data.request_id) {
                var sendmsg = send_message_to_object(data.varname, data.message);
                if (sendmsg.success) {
                    respond_success(data.request_id, sendmsg);
                } else {
                    respond_error(data.request_id, sendmsg.error.code, sendmsg.error.message, sendmsg.error.hint, sendmsg.error.recoverable, sendmsg.error.details);
                }
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing varname, message, or request_id for send_message_to_object");
            }
            break;
        case "send_bang_to_object":
            if (data.varname && data.request_id) {
                var bang = send_bang_to_object(data.varname);
                if (bang.success) {
                    respond_success(data.request_id, bang);
                } else {
                    respond_error(data.request_id, bang.error.code, bang.error.message, bang.error.hint, bang.error.recoverable, bang.error.details);
                }
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing varname or request_id for send_bang_to_object");
            }
            break;
        case "set_number":
            if (data.varname && data.num !== undefined && data.request_id) {
                var num = set_number(data.varname, data.num);
                if (num.success) {
                    respond_success(data.request_id, num);
                } else {
                    respond_error(data.request_id, num.error.code, num.error.message, num.error.hint, num.error.recoverable, num.error.details);
                }
            }
            else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing varname, num, or request_id for set_number");
            }
            break;
        case "create_subpatcher":
            if (data.position && data.varname && data.request_id) {
                var created = create_subpatcher(data.position[0], data.position[1], data.name || "subpatch", data.varname);
                if (created.success) {
                    respond_success(data.request_id, created);
                } else {
                    respond_error(data.request_id, created.error.code, created.error.message, created.error.hint, created.error.recoverable, created.error.details);
                }
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing position, varname, or request_id for create_subpatcher");
            }
            break;
        case "enter_subpatcher":
            if (data.varname && data.request_id) {
                var entered = enter_subpatcher(data.varname);
                if (entered.success) {
                    respond_success(data.request_id, entered);
                } else {
                    respond_error(data.request_id, entered.error.code, entered.error.message, entered.error.hint, entered.error.recoverable, entered.error.details);
                }
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing varname or request_id for enter_subpatcher");
            }
            break;
        case "exit_subpatcher":
            if (data.request_id) {
                var exited = exit_subpatcher();
                respond_success(data.request_id, exited);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id for exit_subpatcher");
            }
            break;
        case "get_patcher_context":
            if (data.request_id) {
                get_patcher_context(data.request_id);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id for get_patcher_context");
            }
            break;
        case "add_subpatcher_io":
            if (data.io_type && data.position && data.varname && data.request_id) {
                var io = add_subpatcher_io(data.position[0], data.position[1], data.io_type, data.varname, data.comment || "");
                if (io.success) {
                    respond_success(data.request_id, io);
                } else {
                    respond_error(data.request_id, io.error.code, io.error.message, io.error.hint, io.error.recoverable, io.error.details);
                }
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing io_type, position, varname, or request_id for add_subpatcher_io");
            }
            break;
        case "get_object_connections":
            if (data.request_id && data.varname) {
                get_object_connections(data.request_id, data.varname);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id or varname for get_object_connections");
            }
            break;
        case "recreate_with_args":
            if (data.request_id && data.varname && data.new_args !== undefined) {
                recreate_with_args(data.request_id, data.varname, data.new_args);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id, varname, or new_args for recreate_with_args");
            }
            break;
        case "move_object":
            if (data.request_id && data.varname && data.x !== undefined && data.y !== undefined) {
                move_object(data.request_id, data.varname, data.x, data.y);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id, varname, x, or y for move_object");
            }
            break;
        case "autofit_existing":
            if (data.varname && data.request_id) {
                var fit = autofit_existing(data.varname);
                respond_success(data.request_id, fit);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing varname or request_id for autofit_existing");
            }
            break;
        case "encapsulate":
            if (data.request_id && data.varnames && data.subpatcher_name && data.subpatcher_varname) {
                encapsulate(data.request_id, data.varnames, data.subpatcher_name, data.subpatcher_varname);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id, varnames, subpatcher_name, or subpatcher_varname for encapsulate");
            }
            break;
        case "check_signal_safety":
            if (data.request_id) {
                check_signal_safety(data.request_id);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id for check_signal_safety");
            }
            break;
        case "bridge_ping":
            if (data.request_id) {
                bridge_ping(data.request_id);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id for bridge_ping");
            }
            break;
        case "health_ping":
            if (data.request_id) {
                bridge_ping(data.request_id);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id for health_ping");
            }
            break;
        case "capabilities":
            if (data.request_id) {
                send_capabilities(data.request_id);
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id for capabilities");
            }
            break;
        case "set_workspace_target":
            if (data.request_id && data.target_id) {
                var ws = set_workspace_target(
                    data.target_id,
                    data.workspace_varname || "",
                    data.workspace_name || ""
                );
                if (ws.success) {
                    respond_success(data.request_id, ws);
                } else {
                    respond_error(
                        data.request_id,
                        ws.error.code,
                        ws.error.message,
                        ws.error.hint,
                        ws.error.recoverable,
                        ws.error.details
                    );
                }
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id or target_id for set_workspace_target");
            }
            break;
        case "workspace_status":
            if (data.request_id) {
                respond_success(data.request_id, get_workspace_status());
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id for workspace_status");
            }
            break;
        case "apply_topology_snapshot":
            if (data.request_id && data.snapshot) {
                var applied = apply_topology_snapshot(data.snapshot);
                if (applied.success) {
                    respond_success(data.request_id, applied);
                } else {
                    respond_error(
                        data.request_id,
                        applied.error.code,
                        applied.error.message,
                        applied.error.hint,
                        applied.error.recoverable,
                        applied.error.details
                    );
                }
            } else {
                respond_error(data.request_id, "VALIDATION_ERROR", "Missing request_id or snapshot for apply_topology_snapshot");
            }
            break;
        default:
            respond_error(data.request_id, "UNKNOWN_ACTION", "Unknown action: " + data.action);
    }
}

// function fetch_test(request_id) {
// 	var str = get_patcher_objects(request_id)
// 	//outlet(1, request_id)
// }

function bridge_ping(request_id) {
    var context = {
        depth: effective_depth(),
        path: effective_path(),
        is_root: effective_depth() === 0
    };
    respond_success(request_id, {
        ok: true,
        timestamp_ms: Date.now(),
        context: context
    });
}

function _ensure_workspace_patcher(workspace_varname, workspace_name) {
    var obj = root_patcher.getnamed(workspace_varname);
    if (obj) {
        var existing_subpatch = obj.subpatcher();
        if (!existing_subpatch) {
            return {
                success: false,
                error: {
                    code: "PRECONDITION_FAILED",
                    message: "Workspace varname exists but is not a subpatcher: " + workspace_varname,
                    recoverable: false,
                    details: { workspace_varname: workspace_varname }
                }
            };
        }
        return { success: true, patcher: existing_subpatch, created: false };
    }

    var name = workspace_name || workspace_varname || "mcp_workspace";
    var new_obj = root_patcher.newdefault(80, 80, "patcher", name);
    new_obj.varname = workspace_varname;
    var subpatch = new_obj.subpatcher();
    if (!subpatch) {
        return {
            success: false,
            error: {
                code: "INTERNAL_ERROR",
                message: "Failed to create workspace subpatcher for: " + workspace_varname,
                recoverable: false,
                details: { workspace_varname: workspace_varname }
            }
        };
    }
    return { success: true, patcher: subpatch, created: true };
}

function set_workspace_target(target_id, workspace_varname, workspace_name) {
    if (target_id === "host") {
        current_patcher = root_patcher;
        patcher_stack = [];
        base_context_depth = 0;
        active_workspace_target = "host";
        active_workspace_varname = "";
        avoid_rect_called = false;
        return get_workspace_status();
    }

    if (target_id !== "active" && target_id !== "scratch") {
        return {
            success: false,
            error: {
                code: "VALIDATION_ERROR",
                message: "Unsupported target_id: " + target_id,
                recoverable: true,
                details: { target_id: target_id }
            }
        };
    }

    if (!workspace_varname) {
        return {
            success: false,
            error: {
                code: "VALIDATION_ERROR",
                message: "workspace_varname is required for target: " + target_id,
                recoverable: true,
                details: { target_id: target_id }
            }
        };
    }

    var ensured = _ensure_workspace_patcher(workspace_varname, workspace_name);
    if (!ensured.success) {
        return ensured;
    }

    patcher_stack = [{ patcher: root_patcher, name: workspace_varname }];
    current_patcher = ensured.patcher;
    base_context_depth = 1;
    active_workspace_target = target_id;
    active_workspace_varname = workspace_varname;
    avoid_rect_called = false;

    var status = get_workspace_status();
    status.created_workspace = !!ensured.created;
    return status;
}

function get_workspace_status() {
    return {
        success: true,
        target_id: active_workspace_target,
        workspace_varname: active_workspace_varname,
        depth: effective_depth(),
        path: effective_path(),
        is_root: effective_depth() === 0,
        base_context_depth: base_context_depth
    };
}

function sanitize_snapshot_value(value, depth) {
    if (depth > 4) {
        return null;
    }
    if (value === null || value === undefined) {
        return null;
    }
    var t = typeof value;
    if (t === "number" || t === "string" || t === "boolean") {
        return value;
    }
    if (Array.isArray(value)) {
        var arr = [];
        for (var i = 0; i < value.length; i++) {
            arr.push(sanitize_snapshot_value(value[i], depth + 1));
        }
        return arr;
    }
    if (t === "object") {
        var out = {};
        var keys = [];
        try {
            keys = Object.keys(value);
        } catch (e) {
            keys = [];
        }
        for (var k = 0; k < keys.length; k++) {
            var key = keys[k];
            try {
                out[key] = sanitize_snapshot_value(value[key], depth + 1);
            } catch (e) {
                // Skip non-readable members.
            }
        }
        if (Object.keys(out).length > 0) {
            return out;
        }
        try {
            return String(value);
        } catch (e) {
            return null;
        }
    }
    try {
        return String(value);
    } catch (e) {
        return null;
    }
}

function collect_serializable_attributes(obj) {
    var attributes = {};
    var attrnames = [];
    try {
        attrnames = obj.getattrnames();
    } catch (e) {
        attrnames = [];
    }

    if (!attrnames || !attrnames.length) {
        return attributes;
    }
    for (var i = 0; i < attrnames.length; i++) {
        var name = attrnames[i];
        try {
            attributes[name] = sanitize_snapshot_value(obj.getattr(name), 0);
        } catch (e) {
            // Skip attributes that cannot be serialized in this runtime context.
        }
    }
    return attributes;
}

function apply_snapshot_attributes(obj, attributes) {
    if (!attributes || typeof attributes !== "object") {
        return { applied: 0, skipped: 0 };
    }
    var blocked = {
        "varname": true,
        "maxclass": true,
        "boxtext": true,
        "text": true,
        "patching_rect": true,
        "rect": true,
        "numinlets": true,
        "numoutlets": true
    };
    var applied = 0;
    var skipped = 0;
    var names = Object.keys(attributes);
    for (var i = 0; i < names.length; i++) {
        var name = names[i];
        if (blocked[name]) {
            skipped++;
            continue;
        }
        try {
            obj.setattr(name, attributes[name]);
            applied++;
        } catch (e) {
            skipped++;
        }
    }
    return { applied: applied, skipped: skipped };
}

function apply_topology_snapshot(snapshot) {
    if (!snapshot || !snapshot.boxes || !snapshot.lines) {
        return {
            success: false,
            error: {
                code: "VALIDATION_ERROR",
                message: "Snapshot must include boxes and lines arrays.",
                recoverable: true,
                details: {}
            }
        };
    }

    var existing = [];
    current_patcher.apply(function(obj) {
        if (obj.maxclass && obj.maxclass !== "patchline") {
            existing.push(obj);
        }
    });
    for (var i = 0; i < existing.length; i++) {
        current_patcher.remove(existing[i]);
    }

    var created = {};
    var created_count = 0;
    var skipped_boxes = 0;
    var restored_rects = 0;
    var attributes_applied = 0;
    var attributes_skipped = 0;

    for (var b = 0; b < snapshot.boxes.length; b++) {
        var row = snapshot.boxes[b];
        var box = row && row.box ? row.box : row;
        if (!box || typeof box !== "object") {
            skipped_boxes++;
            continue;
        }

        var rect = box.patching_rect || [100, 100, 160, 122];
        var x = rect[0];
        var y = rect[1];
        var boxtext = box.boxtext || box.text;
        var maxclass = box.maxclass;
        var varname = box.varname;

        var new_obj = null;
        try {
            if (boxtext && typeof boxtext === "string") {
                new_obj = current_patcher.newdefault(x, y, boxtext);
            } else if (maxclass && typeof maxclass === "string") {
                new_obj = current_patcher.newdefault(x, y, maxclass);
            } else {
                skipped_boxes++;
                continue;
            }
            if (varname && typeof varname === "string") {
                new_obj.varname = varname;
                created[varname] = new_obj;
            } else {
                var generated = "restored_" + b + "_" + Math.floor(Math.random() * 100000);
                new_obj.varname = generated;
                created[generated] = new_obj;
            }
            if (rect && rect.length >= 4) {
                try {
                    new_obj.rect = [rect[0], rect[1], rect[2], rect[3]];
                    restored_rects++;
                } catch (e) {
                    // Keep object even if rect assignment fails.
                }
            }
            var attr_result = apply_snapshot_attributes(new_obj, box.attributes);
            attributes_applied += attr_result.applied;
            attributes_skipped += attr_result.skipped;
            created_count++;
        } catch (e) {
            skipped_boxes++;
        }
    }

    var connected = 0;
    var skipped_lines = 0;
    for (var l = 0; l < snapshot.lines.length; l++) {
        var lineRow = snapshot.lines[l];
        var patchline = lineRow && lineRow.patchline ? lineRow.patchline : lineRow;
        if (!patchline || typeof patchline !== "object") {
            skipped_lines++;
            continue;
        }
        var source = patchline.source || [];
        var destination = patchline.destination || [];
        if (source.length < 2 || destination.length < 2) {
            skipped_lines++;
            continue;
        }
        var srcVar = source[0];
        var srcOutlet = source[1];
        var dstVar = destination[0];
        var dstInlet = destination[1];
        var srcObj = created[srcVar];
        var dstObj = created[dstVar];
        if (!srcObj || !dstObj) {
            skipped_lines++;
            continue;
        }
        try {
            current_patcher.connect(srcObj, srcOutlet, dstObj, dstInlet);
            connected++;
        } catch (e) {
            skipped_lines++;
        }
    }

    avoid_rect_called = false;
    return {
        success: true,
        restored_boxes: created_count,
        restored_lines: connected,
        skipped_boxes: skipped_boxes,
        skipped_lines: skipped_lines,
        restored_rects: restored_rects,
        attributes_applied: attributes_applied,
        attributes_skipped: attributes_skipped
    };
}

function send_capabilities(request_id) {
    respond_success(request_id, {
        protocol_version: "2.0",
        health_ping: true,
        bridge_ping: true,
        capabilities: true,
        set_workspace_target: true,
        workspace_status: true,
        apply_topology_snapshot: true,
        supported_actions: [
            "get_objects_in_patch", "get_objects_in_selected", "get_object_attributes",
            "get_avoid_rect_position", "add_object", "remove_object",
            "connect_objects", "disconnect_objects", "set_object_attribute",
            "set_message_text", "send_message_to_object", "send_bang_to_object",
            "set_number", "create_subpatcher", "enter_subpatcher", "exit_subpatcher",
            "get_patcher_context", "add_subpatcher_io", "get_object_connections",
            "recreate_with_args", "move_object", "autofit_existing", "encapsulate",
            "check_signal_safety", "bridge_ping", "health_ping", "capabilities",
            "set_workspace_target", "workspace_status", "apply_topology_snapshot"
        ],
        supports_auth: true,
        supports_idempotency: true,
        notes: "Envelope and legacy request payloads are accepted."
    });
}

// Objects that need float formatting to avoid integer truncation
var FLOAT_SENSITIVE_OBJECTS = {
    "+": true, "-": true, "*": true, "/": true, "%": true,
    "pow": true, "scale": true,
    "pack": true, "pak": true, "unpack": true
};

// Format a number with decimal point to ensure Max interprets as float
function format_float_arg(arg) {
    if (typeof arg === "number") {
        var s = arg.toString();
        // Add decimal point if it's a whole number
        if (s.indexOf(".") === -1 && s.indexOf("e") === -1) {
            return s + ".";
        }
        return s;
    }
    // If it's a string that looks like a float indicator (e.g., "1500."), preserve it
    if (typeof arg === "string") {
        return arg;
    }
    return String(arg);
}

function add_object(x, y, type, args, var_name, request_id) {
    // Preflight check: require get_avoid_rect_position to be called first
    if (!avoid_rect_called) {
        respond_error(
            request_id,
            "PRECONDITION_FAILED",
            "PREFLIGHT REQUIRED: Call get_avoid_rect_position() before placing objects.",
            "Call get_avoid_rect_position() immediately before add_object().",
            true
        );
        return;
    }

    var new_obj;

    // For float-sensitive objects, construct boxtext manually to preserve decimal points
    if (FLOAT_SENSITIVE_OBJECTS[type] && args.length > 0) {
        // Build boxtext with proper float formatting
        var formatted_args = [];
        for (var i = 0; i < args.length; i++) {
            formatted_args.push(format_float_arg(args[i]));
        }
        // Pass entire boxtext as classname - Max parses the whole string
        var boxtext = type + " " + formatted_args.join(" ");
        new_obj = current_patcher.newdefault(x, y, boxtext);
    } else {
        new_obj = current_patcher.newdefault(x, y, type, args);
    }

    // Check for jbogus - object doesn't exist
    if (new_obj.maxclass === "jbogus") {
        current_patcher.remove(new_obj);
        respond_error(
            request_id,
            "VALIDATION_ERROR",
            "OBJECT DOES NOT EXIST: '" + type + "' is not a valid Max object.",
            null,
            true,
            { obj_type: type }
        );
        return;
    }

    new_obj.varname = var_name;
    if (type == "message" || type == "comment" || type == "flonum") {
        new_obj.message("set", args);
    }
    // Auto-fit width based on text content
    autofit_object(new_obj, type, args);

    // Note: Integer type checking for math/pack/unpack objects is now handled
    // in server.py with proper errors before requests reach this code.

    var warnings = [];

    // Check for large patch (every N objects)
    var large_patch_warning = check_large_patch_warning();
    if (large_patch_warning) {
        warnings.push(large_patch_warning);
    }

    // Check if this is an MSP object and if we should run signal safety check
    var do_signal_safety = false;
    if (type.charAt(type.length - 1) === "~") {
        msp_objects_counter++;
        if (msp_objects_counter >= MSP_SAFETY_CHECK_INTERVAL) {
            msp_objects_counter = 0;
            do_signal_safety = true;
        }
    }

    if (do_signal_safety) {
        // Route to signal safety check, which will send the response
        run_signal_safety_for_add_object(request_id, warnings);
    } else {
        // Send success response
        var response = warnings.length > 0 ? "ok - " + warnings.join(" | ") : "ok";
        respond_success(request_id, response);
    }
}

function run_signal_safety_for_add_object(request_id, existing_warnings) {
    var warnings = [];
    var signal_objects = {};
    var signal_connections = [];
    var objects_to_check = [];

    // Collect signal objects and connections
    current_patcher.apply(function(obj) {
        var mc = obj.maxclass;
        if (!mc || mc === "patchline") return;

        if (mc.charAt(mc.length - 1) === "~") {
            var vn = obj.varname;
            if (!vn) {
                vn = "sig-" + Math.floor(Math.random() * 100000);
                obj.varname = vn;
            }
            signal_objects[vn] = { maxclass: mc, varname: vn };

            if (mc === "*~" || mc === "comb~") {
                objects_to_check.push(vn);
            }

            var out_cords = obj.patchcords.outputs;
            if (out_cords) {
                for (var i = 0; i < out_cords.length; i++) {
                    var dst = out_cords[i].dstobject;
                    var dst_mc = dst.maxclass;
                    if (dst_mc && dst_mc.charAt(dst_mc.length - 1) === "~") {
                        var dst_vn = dst.varname || "sig-" + Math.floor(Math.random() * 100000);
                        if (!dst.varname) dst.varname = dst_vn;
                        signal_connections.push({
                            src_varname: vn, src_maxclass: mc,
                            dst_varname: dst_vn, dst_maxclass: dst_mc
                        });
                    }
                }
            }
        }
    });

    // Build adjacency list for cycle detection
    var adj = {};
    for (var i = 0; i < signal_connections.length; i++) {
        var conn = signal_connections[i];
        if (!adj[conn.src_varname]) adj[conn.src_varname] = [];
        adj[conn.src_varname].push({ dst_varname: conn.dst_varname, dst_maxclass: conn.dst_maxclass });
    }

    // Detect feedback loops
    var visited = {};
    var rec_stack = {};

    function detect_cycle(node, path) {
        if (rec_stack[node]) {
            var cycle_start = path.indexOf(node);
            var cycle_path = path.slice(cycle_start);
            var has_tapin = false, tapin_is_direct = false;

            for (var i = 0; i < cycle_path.length; i++) {
                var curr_obj = signal_objects[cycle_path[i]];
                if (curr_obj && curr_obj.maxclass === "tapin~") {
                    has_tapin = true;
                    var prev_idx = (i === 0) ? cycle_path.length - 1 : i - 1;
                    var prev_obj = signal_objects[cycle_path[prev_idx]];
                    if (prev_obj && prev_obj.maxclass === "tapout~") tapin_is_direct = true;
                }
            }

            if (!(has_tapin && tapin_is_direct)) {
                warnings.push({ type: "FEEDBACK_LOOP", message: "Dangerous feedback loop detected", objects: cycle_path });
            }
            return;
        }
        if (visited[node]) return;
        visited[node] = true;
        rec_stack[node] = true;
        path.push(node);
        var neighbors = adj[node] || [];
        for (var i = 0; i < neighbors.length; i++) {
            detect_cycle(neighbors[i].dst_varname, path.slice());
        }
        rec_stack[node] = false;
    }

    for (var vn in signal_objects) {
        if (!visited[vn]) detect_cycle(vn, []);
    }

    // Check for missing limiter before dac~
    var has_dac = false;
    var limiter_types = ["clip~", "tanh~", "saturate~", "limiter~", "limi~", "omx.peaklim~", "omx.comp~"];
    var has_limiter = false;

    for (var vn in signal_objects) {
        var mc = signal_objects[vn].maxclass;
        if (mc === "dac~") has_dac = true;
        if (limiter_types.indexOf(mc) !== -1) has_limiter = true;
    }

    if (has_dac && !has_limiter) {
        warnings.push({ type: "NO_LIMITER", message: "No limiter before dac~. Consider adding clip~ or tanh~." });
    }

    // Route to v8 for gain/feedback arg checking
    var check_data = {
        request_id: request_id,
        is_add_object_response: true,
        existing_warnings: existing_warnings,
        signal_warnings: warnings,
        objects_to_check: objects_to_check
    };
    outlet(2, "complete_signal_safety", JSON.stringify(check_data));
}

// Character width lookup for Arial 12pt (slightly wider to prevent wrapping)
function get_text_width(text) {
    var very_narrow = "il|!.,;:'`1";      // ~4px
    var narrow = "jtfr()-[]{}/ -";         // ~5px
    var medium = "aceszvxyknuhbdgpq023456789"; // ~7px
    var wide = "mwMW@%";                   // ~10px
    // Everything else (uppercase, ~, *, +, etc.): ~8px

    var width = 0;
    for (var i = 0; i < text.length; i++) {
        var c = text[i];
        if (very_narrow.indexOf(c) !== -1) {
            width += 4;
        } else if (narrow.indexOf(c) !== -1) {
            width += 5;
        } else if (medium.indexOf(c) !== -1) {
            width += 7;
        } else if (wide.indexOf(c) !== -1) {
            width += 10;
        } else {
            width += 8;  // default for uppercase, symbols like ~ * +
        }
    }
    return width;
}

function autofit_object(obj, type, args) {
    // Hard skip for inlets/outlets - never resize these
    if (type === "inlet" || type === "outlet") {
        return;
    }

    // Skip UI objects that should keep default sizes
    var skip_types = ["toggle", "button", "slider", "dial", "number", "flonum",
                      "kslider", "panel", "live.dial", "live.slider", "live.toggle",
                      "live.button", "live.numbox", "live.menu", "meter~", "spectroscope~",
                      "gain~", "levelmeter~", "multislider", "matrixctrl", "nodes"];
    if (skip_types.indexOf(type) !== -1) {
        return; // Keep default size
    }

    // Message boxes get fixed 70px width
    if (type === "message") {
        var rect = obj.rect;
        var height = rect[3] - rect[1];
        obj.rect = [rect[0], rect[1], rect[0] + 70, rect[1] + height];
        return;
    }

    // Auto-size: objects and comments
    var text = type;
    if (args && args.length > 0) {
        // Handle array args - join with spaces
        if (Array.isArray(args)) {
            text = type + " " + args.join(" ");
        } else {
            text = type + " " + String(args);
        }
    }

    // Calculate width using character lookup + box padding
    var box_padding = 16;
    var min_width = 32;
    var text_width = get_text_width(text);
    var calculated_width = Math.max(min_width, text_width + box_padding);

    // Get current rect and update width
    var rect = obj.rect;
    var height = rect[3] - rect[1]; // preserve height
    obj.rect = [rect[0], rect[1], rect[0] + calculated_width, rect[1] + height];
}

function remove_object(var_name) {
	var obj = current_patcher.getnamed(var_name);
    if (!obj) {
        return {
            success: false,
            error: {
                code: "OBJECT_NOT_FOUND",
                message: "Object not found: " + var_name,
                recoverable: true,
                details: { varname: var_name }
            }
        };
    }
    current_patcher.remove(obj);
    return { success: true, varname: var_name };
}

function connect_objects(src_varname, outlet_idx, dst_varname, inlet_idx) {
    var src = current_patcher.getnamed(src_varname);
    var dst = current_patcher.getnamed(dst_varname);
    if (!src || !dst) {
        return {
            success: false,
            error: {
                code: "OBJECT_NOT_FOUND",
                message: "Source or destination object not found for connect.",
                recoverable: true,
                details: { src_varname: src_varname, dst_varname: dst_varname }
            }
        };
    }
    current_patcher.connect(src, outlet_idx, dst, inlet_idx);
    return { success: true, src_varname: src_varname, dst_varname: dst_varname, outlet_idx: outlet_idx, inlet_idx: inlet_idx };
}

function disconnect_objects(src_varname, outlet_idx, dst_varname, inlet_idx) {
	var src = current_patcher.getnamed(src_varname);
    var dst = current_patcher.getnamed(dst_varname);
    if (!src || !dst) {
        return {
            success: false,
            error: {
                code: "OBJECT_NOT_FOUND",
                message: "Source or destination object not found for disconnect.",
                recoverable: true,
                details: { src_varname: src_varname, dst_varname: dst_varname }
            }
        };
    }
	current_patcher.disconnect(src, outlet_idx, dst, inlet_idx);
    return { success: true, src_varname: src_varname, dst_varname: dst_varname, outlet_idx: outlet_idx, inlet_idx: inlet_idx };
}

function set_object_attribute(varname, attr_name, attr_value) {
    var obj = current_patcher.getnamed(varname);
    if (!obj) {
        return {
            success: false,
            error: {
                code: "OBJECT_NOT_FOUND",
                message: "Object not found: " + varname,
                recoverable: true,
                details: { varname: varname }
            }
        };
    }
    if (obj.maxclass == "message" || obj.maxclass == "comment") {
        if (attr_name == "text") {
            obj.message("set", attr_value);
            return { success: true, varname: varname, attr_name: attr_name };
        }
    }
    // Check if the attribute exists before setting it
    var attrnames = obj.getattrnames();
    if (attrnames.indexOf(attr_name) == -1) {
        return {
            success: false,
            error: {
                code: "VALIDATION_ERROR",
                message: "Attribute not found: " + attr_name,
                recoverable: true,
                details: { varname: varname, attr_name: attr_name }
            }
        };
    }
    // Set the attribute
    obj.setattr(attr_name, attr_value);
    return { success: true, varname: varname, attr_name: attr_name };
}

function set_message_text(varname, new_text) {
    var obj = current_patcher.getnamed(varname);
    if (!obj) {
        return {
            success: false,
            error: {
                code: "OBJECT_NOT_FOUND",
                message: "Object not found: " + varname,
                recoverable: true,
                details: { varname: varname }
            }
        };
    }
    if (obj.maxclass == "message") {
        obj.message("set", new_text);
        return { success: true, varname: varname };
    }
    return {
        success: false,
        error: {
            code: "VALIDATION_ERROR",
            message: "Object is not a message box: " + varname,
            recoverable: true,
            details: { varname: varname, maxclass: obj.maxclass }
        }
    };
}

function send_message_to_object(varname, message) {
    var obj = current_patcher.getnamed(varname);
    if (!obj) {
        return {
            success: false,
            error: {
                code: "OBJECT_NOT_FOUND",
                message: "Object not found: " + varname,
                recoverable: true,
                details: { varname: varname }
            }
        };
    }
    obj.message(message);
    return { success: true, varname: varname };
}

function send_bang_to_object(varname) {
    var obj = current_patcher.getnamed(varname);
    if (!obj) {
        return {
            success: false,
            error: {
                code: "OBJECT_NOT_FOUND",
                message: "Object not found: " + varname,
                recoverable: true,
                details: { varname: varname }
            }
        };
    }
    obj.message("bang");
    return { success: true, varname: varname };
}

function set_text_in_comment(varname, text) {
    var obj = p.getnamed(varname);
    if (obj) {
        if (obj.maxclass == "comment") {
            obj.message("set", text);
        } else {
            post("Object is not a comment box: " + varname);
        }
    } else {
        post("Object not found: " + varname);
    }
}

function set_number(varname, num) {
    var obj = current_patcher.getnamed(varname);
    if (!obj) {
        return {
            success: false,
            error: {
                code: "OBJECT_NOT_FOUND",
                message: "Object not found: " + varname,
                recoverable: true,
                details: { varname: varname }
            }
        };
    }
    obj.message("set", num);
    return { success: true, varname: varname, value: num };
}

// ========================================
// Subpatcher navigation functions:

function create_subpatcher(x, y, name, var_name) {
    var new_obj = current_patcher.newdefault(x, y, "patcher", name);
    new_obj.varname = var_name;
    post("Created subpatcher: " + var_name + " (" + name + ")\n");
    return { success: true, varname: var_name, name: name, position: [x, y] };
}

function enter_subpatcher(var_name) {
    var obj = current_patcher.getnamed(var_name);
    if (!obj) {
        return {
            success: false,
            error: {
                code: "OBJECT_NOT_FOUND",
                message: "Object not found: " + var_name,
                recoverable: true,
                details: { varname: var_name }
            }
        };
    }

    var subpatch = obj.subpatcher();
    if (!subpatch) {
        return {
            success: false,
            error: {
                code: "VALIDATION_ERROR",
                message: "Object is not a subpatcher: " + var_name,
                recoverable: true,
                details: { varname: var_name }
            }
        };
    }

    // Push current context onto stack
    patcher_stack.push({
        patcher: current_patcher,
        name: var_name
    });

    // Navigate into subpatcher
    current_patcher = subpatch;

    // Reset preflight check - new context requires new avoid rect check
    avoid_rect_called = false;

    post("Entered subpatcher: " + var_name + " (depth: " + effective_depth() + ")\n");
    return { success: true, depth: effective_depth(), path: effective_path() };
}

function exit_subpatcher() {
    if (patcher_stack.length <= base_context_depth) {
        post("Already at root patcher\n");
        return { success: true, depth: 0, no_op: true };
    }

    var context = patcher_stack.pop();
    current_patcher = context.patcher;

    // Reset preflight check - returning to parent context requires new avoid rect check
    avoid_rect_called = false;

    post("Exited to parent patcher (depth: " + effective_depth() + ")\n");
    return { success: true, depth: effective_depth(), path: effective_path() };
}

function get_patcher_context(request_id) {
    var context = {
        depth: effective_depth(),
        path: effective_path(),
        is_root: (effective_depth() == 0),
        target_id: active_workspace_target,
        workspace_varname: active_workspace_varname
    };

    respond_success(request_id, context);
}

function add_subpatcher_io(x, y, io_type, var_name, comment) {
    // io_type should be "inlet" or "outlet" (they auto-detect signal vs message)
    if (io_type != "inlet" && io_type != "outlet") {
        return {
            success: false,
            error: {
                code: "VALIDATION_ERROR",
                message: "Invalid io_type: " + io_type + ". Use inlet or outlet (no ~ needed, they auto-detect)",
                recoverable: true,
                details: { io_type: io_type }
            }
        };
    }

    var new_obj = current_patcher.newdefault(x, y, io_type);
    new_obj.varname = var_name;

    if (comment) {
        new_obj.setattr("comment", comment);
    }

    post("Created " + io_type + ": " + var_name + "\n");
    return { success: true, io_type: io_type, varname: var_name, position: [x, y] };
}

// ========================================
// fetch request:

function get_objects_in_patch(request_id) {
    obj_count = 0;
    boxes = [];
    lines = [];

    // Use apply (not applydeep) to only get objects in current patcher, not nested
    current_patcher.apply(collect_objects);
    var patcher_dict = {};
    patcher_dict["boxes"] = boxes;
    patcher_dict["lines"] = lines;

    // use these if no v8:
    // var results = {"request_id": request_id, "results": patcher_dict}
    // outlet(1, "response", split_long_string(JSON.stringify(results, null, 2), 2000));

    // use this if has v8:
    outlet(2, "add_boxtext", request_id, JSON.stringify(patcher_dict, null, 0));
}

function get_objects_in_selected(request_id) {
    obj_count = 0;
    boxes = [];
    lines = [];

    current_patcher.applyif(collect_objects, function (obj) {
        return obj.selected;
    });
    var patcher_dict = {};
    patcher_dict["boxes"] = boxes;
    patcher_dict["lines"] = lines;

    // use these if no v8:
    // var results = {"request_id": request_id, "results": patcher_dict}
    // outlet(1, "response", split_long_string(JSON.stringify(results, null, 2), 2000));

    // use this if has v8:
    outlet(2, "add_boxtext", request_id, JSON.stringify(patcher_dict, null, 0));
}

function collect_objects(obj) {
    //var keys = Object.keys(obj.varname);
    //post(typeof obj.varname + "\n");
    if (obj.varname && obj.varname.substring(0, 8) == "maxmcpid"){
        return;
    }
    if (!obj.varname){
        obj.varname = "obj-" + obj_count;
    }
    obj_count+=1;

    var outputs = obj.patchcords.outputs;
    if (outputs.length){
        for (var i = 0; i < outputs.length; i++) {
            lines.push({patchline: {
                source: [obj.varname, outputs[i].srcoutlet],
                destination: [outputs[i].dstobject.varname, outputs[i].dstinlet]
            }})
        }
    }
    var attr = collect_serializable_attributes(obj);
    var boxtext = null;
    try {
        boxtext = obj.boxtext;
    } catch (e) {
        boxtext = null;
    }

    boxes.push({box:{
        maxclass: obj.maxclass,
        varname: obj.varname,
        patching_rect: obj.rect,
        numinlets: obj.patchcords && obj.patchcords.inputs ? obj.patchcords.inputs.length : 0,
        numoutlets: obj.patchcords && obj.patchcords.outputs ? obj.patchcords.outputs.length : 0,
        boxtext: boxtext,
        attributes: attr,
    }})
}

function get_object_attributes(request_id, var_name) {
    var obj = current_patcher.getnamed(var_name);
    if (!obj) {
        respond_error(
            request_id,
            "OBJECT_NOT_FOUND",
            "Object not found: " + var_name,
            null,
            true,
            { varname: var_name }
        );
        return;
    }
    var attributes = collect_serializable_attributes(obj);

    // use these if no v8:
    // var results = {"request_id": request_id, "results": patcher_dict}
    // outlet(1, "response", split_long_string(JSON.stringify(results, null, 2), 2000));

    // use this if has v8:
    respond_success(request_id, attributes);
}

function get_window_rect() {
    var w = this.patcher.wind;
    var title = w.title;
    var size = w.size;
    // outlet(1, "response", split_long_string(JSON.stringify(results, null, 0), 2500));
}

function get_avoid_rect_position(request_id) {
    var l, t, r, b;
    var has_rect = false;
    current_patcher.apply(function (obj) {
        // Skip objects without valid rects (like patchlines)
        if (!obj.rect || obj.rect[2] <= obj.rect[0]) {
            return;
        }
        has_rect = true;
        if (obj.rect[0] < l || l == undefined) {
            l = obj.rect[0];
        }
        if (obj.rect[1] < t || t == undefined) {
            t = obj.rect[1];
        }
        if (obj.rect[2] > r || r == undefined) {
            r = obj.rect[2];
        }
        if (obj.rect[3] > b || b == undefined) {
            b = obj.rect[3];
        }
    });
    var avoid_rect = has_rect ? [l, t, r, b] : [0, 0, 0, 0];

    // Mark preflight check as done
    avoid_rect_called = true;

    respond_success(request_id, avoid_rect);
}

// ========================================
// Object manipulation enhancements:

function get_object_connections(request_id, var_name) {
    var obj = current_patcher.getnamed(var_name);
    if (!obj) {
        respond_error(
            request_id,
            "OBJECT_NOT_FOUND",
            "Object not found: " + var_name,
            null,
            true,
            { varname: var_name }
        );
        return;
    }

    var inputs = [];
    var outputs = [];

    // Get output connections (from this object to others)
    var out_cords = obj.patchcords.outputs;
    if (out_cords && out_cords.length) {
        for (var i = 0; i < out_cords.length; i++) {
            outputs.push({
                src_outlet: out_cords[i].srcoutlet,
                dst_varname: out_cords[i].dstobject.varname,
                dst_inlet: out_cords[i].dstinlet
            });
        }
    }

    // Get input connections (from other objects to this one)
    var in_cords = obj.patchcords.inputs;
    if (in_cords && in_cords.length) {
        for (var i = 0; i < in_cords.length; i++) {
            inputs.push({
                src_varname: in_cords[i].srcobject.varname,
                src_outlet: in_cords[i].srcoutlet,
                dst_inlet: in_cords[i].dstinlet
            });
        }
    }

    var connection_info = {
        varname: var_name,
        inputs: inputs,
        outputs: outputs
    };

    respond_success(request_id, connection_info);
}

function recreate_with_args(request_id, var_name, new_args) {
    var obj = current_patcher.getnamed(var_name);
    if (!obj) {
        respond_error(
            request_id,
            "OBJECT_NOT_FOUND",
            "Object not found: " + var_name,
            null,
            true,
            { varname: var_name }
        );
        return;
    }

    // Store object info
    var obj_type = obj.maxclass;
    var rect = obj.rect;
    var x = rect[0];
    var y = rect[1];

    // Store all connections
    var inputs = [];
    var outputs = [];

    var out_cords = obj.patchcords.outputs;
    if (out_cords && out_cords.length) {
        for (var i = 0; i < out_cords.length; i++) {
            outputs.push({
                src_outlet: out_cords[i].srcoutlet,
                dst_varname: out_cords[i].dstobject.varname,
                dst_inlet: out_cords[i].dstinlet
            });
        }
    }

    var in_cords = obj.patchcords.inputs;
    if (in_cords && in_cords.length) {
        for (var i = 0; i < in_cords.length; i++) {
            inputs.push({
                src_varname: in_cords[i].srcobject.varname,
                src_outlet: in_cords[i].srcoutlet,
                dst_inlet: in_cords[i].dstinlet
            });
        }
    }

    // Remove the old object
    current_patcher.remove(obj);

    // Create new object with new args (use float formatting for sensitive objects)
    var new_obj;
    if (FLOAT_SENSITIVE_OBJECTS[obj_type] && new_args.length > 0) {
        var formatted_args = [];
        for (var i = 0; i < new_args.length; i++) {
            formatted_args.push(format_float_arg(new_args[i]));
        }
        var boxtext = obj_type + " " + formatted_args.join(" ");
        new_obj = current_patcher.newdefault(x, y, boxtext);
    } else {
        new_obj = current_patcher.newdefault(x, y, obj_type, new_args);
    }
    new_obj.varname = var_name;

    // Handle special object types
    if (obj_type == "message" || obj_type == "comment" || obj_type == "flonum") {
        new_obj.message("set", new_args);
    }

    // Auto-fit width based on text content
    autofit_object(new_obj, obj_type, new_args);

    // Restore output connections (from this object to others)
    for (var i = 0; i < outputs.length; i++) {
        var dst = current_patcher.getnamed(outputs[i].dst_varname);
        if (dst) {
            current_patcher.connect(new_obj, outputs[i].src_outlet, dst, outputs[i].dst_inlet);
        }
    }

    // Restore input connections (from others to this object)
    for (var i = 0; i < inputs.length; i++) {
        var src = current_patcher.getnamed(inputs[i].src_varname);
        if (src) {
            current_patcher.connect(src, inputs[i].src_outlet, new_obj, inputs[i].dst_inlet);
        }
    }

    respond_success(request_id, {
        "success": true,
        "varname": var_name,
        "obj_type": obj_type,
        "new_args": new_args,
        "restored_inputs": inputs.length,
        "restored_outputs": outputs.length
    });
    post("Recreated " + var_name + " (" + obj_type + ") with args: " + new_args + "\n");
}

function move_object(request_id, var_name, x, y) {
    var obj = current_patcher.getnamed(var_name);
    if (!obj) {
        respond_error(
            request_id,
            "OBJECT_NOT_FOUND",
            "Object not found: " + var_name,
            null,
            true,
            { varname: var_name }
        );
        return;
    }

    // Get current rect to preserve width/height
    var rect = obj.rect;
    var width = rect[2] - rect[0];
    var height = rect[3] - rect[1];

    // Set new position while preserving size
    var new_rect = [x, y, x + width, y + height];
    obj.rect = new_rect;

    respond_success(request_id, {
        "success": true,
        "varname": var_name,
        "old_position": [rect[0], rect[1]],
        "new_position": [x, y]
    });
    post("Moved " + var_name + " to [" + x + ", " + y + "]\n");
}

function autofit_existing(var_name) {
    var obj = current_patcher.getnamed(var_name);
    if (!obj) {
        return {
            success: false,
            error: {
                code: "OBJECT_NOT_FOUND",
                message: "Object not found: " + var_name,
                recoverable: true,
                details: { varname: var_name }
            }
        };
    }
    // Route to v8 add-on which has access to obj.boxtext
    outlet(2, "autofit_v8", var_name);
    return { success: true, varname: var_name };
}

// ========================================
// Signal safety analysis:

function check_signal_safety(request_id) {
    var warnings = [];
    var signal_objects = {};  // varname -> {maxclass, args, boxtext}
    var signal_connections = [];  // {src_varname, src_outlet, dst_varname, dst_inlet}

    // 1. Collect all signal objects and connections
    current_patcher.apply(function(obj) {
        var mc = obj.maxclass;
        if (!mc || mc === "patchline") return;

        // Check if it's a signal object (ends with ~)
        if (mc.charAt(mc.length - 1) === "~") {
            var vn = obj.varname;
            if (!vn) {
                vn = "sig-" + Math.floor(Math.random() * 100000);
                obj.varname = vn;
            }
            signal_objects[vn] = {
                maxclass: mc,
                varname: vn,
                rect: obj.rect
            };

            // Get connections
            var out_cords = obj.patchcords.outputs;
            if (out_cords) {
                for (var i = 0; i < out_cords.length; i++) {
                    var dst = out_cords[i].dstobject;
                    var dst_mc = dst.maxclass;
                    // Only track signal connections
                    if (dst_mc && dst_mc.charAt(dst_mc.length - 1) === "~") {
                        var dst_vn = dst.varname;
                        if (!dst_vn) {
                            dst_vn = "sig-" + Math.floor(Math.random() * 100000);
                            dst.varname = dst_vn;
                        }
                        signal_connections.push({
                            src_varname: vn,
                            src_maxclass: mc,
                            src_outlet: out_cords[i].srcoutlet,
                            dst_varname: dst_vn,
                            dst_maxclass: dst_mc,
                            dst_inlet: out_cords[i].dstinlet
                        });
                    }
                }
            }
        }
    });

    // 2. Collect objects that need arg checking (route to v8)
    var objects_to_check = [];
    for (var vn in signal_objects) {
        var obj = signal_objects[vn];
        if (obj.maxclass === "*~" || obj.maxclass === "comb~") {
            objects_to_check.push(vn);
        }
    }

    // 3. Build adjacency list for cycle detection
    var adj = {};  // src_varname -> [{dst_varname, dst_maxclass}]
    for (var i = 0; i < signal_connections.length; i++) {
        var conn = signal_connections[i];
        if (!adj[conn.src_varname]) {
            adj[conn.src_varname] = [];
        }
        adj[conn.src_varname].push({
            dst_varname: conn.dst_varname,
            dst_maxclass: conn.dst_maxclass,
            src_maxclass: conn.src_maxclass
        });
    }

    // 4. Check for feedback loops (excluding valid tapin~/tapout~ patterns)
    // Valid: tapout~ -> ... -> tapin~ (direct to tapin~ is OK)
    // Invalid: tapout~ -> ... -> something before tapin~ in the chain
    var visited = {};
    var rec_stack = {};

    function detect_cycle(node, path) {
        if (rec_stack[node]) {
            // Found a cycle - check if it's a valid delay feedback
            var cycle_start = path.indexOf(node);
            var cycle_path = path.slice(cycle_start);

            // Check if cycle goes through tapin~
            var has_tapin = false;
            var tapin_is_direct_target = false;

            for (var i = 0; i < cycle_path.length; i++) {
                var curr = cycle_path[i];
                var curr_obj = signal_objects[curr];
                if (curr_obj && curr_obj.maxclass === "tapin~") {
                    has_tapin = true;
                    // Check if the connection TO tapin~ is from tapout~
                    var prev_idx = (i === 0) ? cycle_path.length - 1 : i - 1;
                    var prev = cycle_path[prev_idx];
                    var prev_obj = signal_objects[prev];
                    if (prev_obj && prev_obj.maxclass === "tapout~") {
                        tapin_is_direct_target = true;
                    }
                }
            }

            if (has_tapin && tapin_is_direct_target) {
                // Valid delay feedback - tapout~ connects directly to tapin~
                return false;
            }

            // Invalid feedback loop
            warnings.push({
                type: "FEEDBACK_LOOP",
                message: "Potentially dangerous feedback loop detected",
                objects: cycle_path
            });
            return true;
        }

        if (visited[node]) return false;

        visited[node] = true;
        rec_stack[node] = true;
        path.push(node);

        var neighbors = adj[node] || [];
        for (var i = 0; i < neighbors.length; i++) {
            detect_cycle(neighbors[i].dst_varname, path.slice());
        }

        rec_stack[node] = false;
        return false;
    }

    for (var vn in signal_objects) {
        if (!visited[vn]) {
            detect_cycle(vn, []);
        }
    }

    // 5. Check for missing limiter before dac~
    var has_dac = false;
    var dac_inputs = [];

    for (var i = 0; i < signal_connections.length; i++) {
        var conn = signal_connections[i];
        if (conn.dst_maxclass === "dac~") {
            has_dac = true;
            dac_inputs.push(conn.src_varname);
        }
    }

    if (has_dac) {
        // Check if any limiter objects exist in the patch
        var limiter_types = ["clip~", "tanh~", "saturate~", "limiter~", "omx.peaklim~", "omx.comp~"];
        var has_limiter = false;

        for (var vn in signal_objects) {
            if (limiter_types.indexOf(signal_objects[vn].maxclass) !== -1) {
                has_limiter = true;
                break;
            }
        }

        if (!has_limiter) {
            warnings.push({
                type: "NO_LIMITER",
                message: "No limiter detected before dac~. Consider adding clip~, tanh~, or similar to prevent clipping.",
                suggestion: "Add [clip~ -1. 1.] or [tanh~] before dac~"
            });
        }
    }

    // 6. Route to v8 to check gain/feedback values (needs boxtext access)
    var check_data = {
        request_id: request_id,
        warnings: warnings,
        objects_to_check: objects_to_check,
        signal_objects_count: Object.keys(signal_objects).length,
        signal_connections_count: signal_connections.length
    };
    outlet(2, "complete_signal_safety", JSON.stringify(check_data));
}

// ========================================
// Encapsulate function:

function encapsulate(request_id, varnames, subpatcher_name, subpatcher_varname) {
    // Check if we're at root level - encapsulate only works at root currently
    if (effective_depth() > 0) {
        respond_error(
            request_id,
            "PRECONDITION_FAILED",
            "ENCAPSULATE ERROR: Currently only works at root patcher level. Use exit_subpatcher() to return to root first.",
            "Call exit_subpatcher() until depth is 0.",
            true
        );
        return;
    }

    // 1. Collect objects and validate
    var objects = [];
    var varname_set = {};

    for (var i = 0; i < varnames.length; i++) {
        var vn = varnames[i];
        var obj = current_patcher.getnamed(vn);
        if (!obj) {
            respond_error(
                request_id,
                "OBJECT_NOT_FOUND",
                "Object not found: " + vn,
                null,
                true,
                { varname: vn }
            );
            return;
        }
        varname_set[vn] = true;
        objects.push({
            varname: vn,
            obj: obj,
            maxclass: obj.maxclass,
            rect: obj.rect
        });
    }

    if (objects.length === 0) {
        respond_error(
            request_id,
            "VALIDATION_ERROR",
            "No objects to encapsulate",
            "Pass at least one object varname.",
            true
        );
        return;
    }

    // 2. Analyze connections
    var internal_connections = [];
    var external_inputs = [];
    var external_outputs = [];

    for (var i = 0; i < objects.length; i++) {
        var obj = objects[i].obj;
        var vn = objects[i].varname;

        // Check outputs
        var out_cords = obj.patchcords.outputs;
        if (out_cords) {
            for (var j = 0; j < out_cords.length; j++) {
                var dst_obj = out_cords[j].dstobject;
                var dst_vn = dst_obj.varname;
                // Assign varname if missing
                if (!dst_vn) {
                    dst_vn = "obj-ext-" + Math.floor(Math.random() * 10000);
                    dst_obj.varname = dst_vn;
                }
                if (varname_set[dst_vn]) {
                    internal_connections.push({
                        src_varname: vn,
                        src_outlet: out_cords[j].srcoutlet,
                        dst_varname: dst_vn,
                        dst_inlet: out_cords[j].dstinlet
                    });
                } else {
                    external_outputs.push({
                        src_varname: vn,
                        src_outlet: out_cords[j].srcoutlet,
                        dst_varname: dst_vn,
                        dst_inlet: out_cords[j].dstinlet
                    });
                }
            }
        }

        // Check inputs
        var in_cords = obj.patchcords.inputs;
        if (in_cords) {
            for (var j = 0; j < in_cords.length; j++) {
                var src_obj = in_cords[j].srcobject;
                var src_vn = src_obj.varname;
                // Assign varname if missing
                if (!src_vn) {
                    src_vn = "obj-ext-" + Math.floor(Math.random() * 10000);
                    src_obj.varname = src_vn;
                }
                if (!varname_set[src_vn]) {
                    external_inputs.push({
                        src_varname: src_vn,
                        src_outlet: in_cords[j].srcoutlet,
                        dst_varname: vn,
                        dst_inlet: in_cords[j].dstinlet
                    });
                }
            }
        }
    }

    // 3. Calculate bounding box
    var min_x = Infinity, min_y = Infinity, max_x = -Infinity, max_y = -Infinity;
    for (var i = 0; i < objects.length; i++) {
        var rect = objects[i].rect;
        if (rect[0] < min_x) min_x = rect[0];
        if (rect[1] < min_y) min_y = rect[1];
        if (rect[2] > max_x) max_x = rect[2];
        if (rect[3] > max_y) max_y = rect[3];
    }

    // 4. Create inlet/outlet mappings
    // Group external inputs by (dst_varname, dst_inlet)
    var inlet_map = {};
    var inlet_list = [];

    for (var i = 0; i < external_inputs.length; i++) {
        var ei = external_inputs[i];
        var key = ei.dst_varname + ":" + ei.dst_inlet;
        if (inlet_map[key] === undefined) {
            inlet_map[key] = inlet_list.length;
            inlet_list.push({
                idx: inlet_list.length,
                dst_varname: ei.dst_varname,
                dst_inlet: ei.dst_inlet,
                external: []
            });
        }
        inlet_list[inlet_map[key]].external.push({
            src_varname: ei.src_varname,
            src_outlet: ei.src_outlet
        });
    }

    // Group external outputs by (src_varname, src_outlet)
    var outlet_map = {};
    var outlet_list = [];

    for (var i = 0; i < external_outputs.length; i++) {
        var eo = external_outputs[i];
        var key = eo.src_varname + ":" + eo.src_outlet;
        if (outlet_map[key] === undefined) {
            outlet_map[key] = outlet_list.length;
            outlet_list.push({
                idx: outlet_list.length,
                src_varname: eo.src_varname,
                src_outlet: eo.src_outlet,
                external: []
            });
        }
        outlet_list[outlet_map[key]].external.push({
            dst_varname: eo.dst_varname,
            dst_inlet: eo.dst_inlet
        });
    }

    // 5. Create subpatcher at top-left of bounding box
    var sub_obj = current_patcher.newdefault(min_x, min_y, "patcher", subpatcher_name);
    sub_obj.varname = subpatcher_varname;
    var subpatch = sub_obj.subpatcher();

    // 6. Calculate internal layout
    var internal_offset_x = 50;
    var internal_offset_y = 50 + (inlet_list.length > 0 ? 40 : 0);
    var outlet_y = (max_y - min_y) + internal_offset_y + 50;

    // 7. Create inlets at top of subpatcher
    var inlet_varnames = [];
    for (var i = 0; i < inlet_list.length; i++) {
        var inlet_x = 50 + i * 80;
        var inlet_vn = "_inlet_" + i;
        var inlet_obj = subpatch.newdefault(inlet_x, 30, "inlet");
        inlet_obj.varname = inlet_vn;
        inlet_varnames.push(inlet_vn);
    }

    // 8. Create outlets at bottom
    var outlet_varnames = [];
    for (var i = 0; i < outlet_list.length; i++) {
        var outlet_x = 50 + i * 80;
        var outlet_vn = "_outlet_" + i;
        var outlet_obj = subpatch.newdefault(outlet_x, outlet_y, "outlet");
        outlet_obj.varname = outlet_vn;
        outlet_varnames.push(outlet_vn);
    }

    // 9. Recreate objects inside subpatcher
    // We need boxtext to get the full object specification - route through v8
    var objects_info = [];
    for (var i = 0; i < objects.length; i++) {
        var o = objects[i];
        objects_info.push({
            varname: o.varname,
            maxclass: o.maxclass,
            rect: o.rect,
            new_x: (o.rect[0] - min_x) + internal_offset_x,
            new_y: (o.rect[1] - min_y) + internal_offset_y
        });
    }

    // Send to v8 to complete encapsulation with boxtext access
    var encap_data = {
        request_id: request_id,
        subpatcher_varname: subpatcher_varname,
        objects_info: objects_info,
        internal_connections: internal_connections,
        inlet_list: inlet_list,
        outlet_list: outlet_list,
        inlet_varnames: inlet_varnames,
        outlet_varnames: outlet_varnames,
        varname_set: varname_set
    };
    outlet(2, "complete_encapsulate", JSON.stringify(encap_data));
}

// ========================================
// for debugging use only:


function remove_varname() {
    // for debugging
    // remove all objects' varname
    var p = max.frontpatcher;
    p.applydeep(function (obj) {
        obj.varname = "";
    });
}

function assign_mcp_identifier_to_all_objects() {
    // for debugging
    // remove all objects' varname
	var idx = 0
    var p = max.frontpatcher;
    p.applydeep(function (obj) {
        obj.varname = "maxmcpid-"+idx;
		idx += 1
    });
}


function print_varname() {
    // for debugging
    // remove all objects' varname
    var p = max.frontpatcher;
    p.applydeep(function (obj) {
        post(obj.varname)
    });
}

function parsed_patcher() {
	if (max.frontpatcher.filepath == ""){
		post(NOT_SAVED);
		return;
	}
	var lines = new String();
    var patcher_file = new File(max.frontpatcher.filepath);
    //post("max.frontpatcher.filepath: " + patcher_file + "\n");

	while (patcher_file.position != patcher_file.eof){
		lines += patcher_file.readline();
	}
	patcher_file.close();

    var parsed_patcher = JSON.parse(lines);
	// post(JSON.stringify(parsed_patcher));
}
