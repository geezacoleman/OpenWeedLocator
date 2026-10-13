"""Pure helpers for mapping weed detections to relay lanes."""


def relay_ids_for_detections(boxes, weed_centres, lane_width, relay_num,
                             actuation_y_thresh=0, mode='centre'):
    """Return deduplicated relay IDs for detections in the actuation zone.

    ``centre`` mode preserves the original behaviour. ``edge`` mode samples the
    left edge, centre, and right edge of each bounding box so large weeds can
    actuate more than one lane. Boxes use the common OWL ``[x, y, w, h]``
    format; detections without a matching valid box fall back to centre mode.
    """
    if not weed_centres or lane_width <= 0 or relay_num <= 0:
        return []

    fired = set()
    use_edges = mode == 'edge'

    for index, centre in enumerate(weed_centres):
        if len(centre) < 2 or centre[1] < actuation_y_thresh:
            continue

        x_samples = [centre[0]]
        if use_edges and index < len(boxes):
            box = boxes[index]
            if len(box) >= 4 and box[2] > 0:
                start_x = box[0]
                # OWL boxes use an exclusive x + width endpoint. Sample the
                # final covered pixel so a box ending on a lane boundary does
                # not incorrectly actuate the lane to its right.
                end_x = start_x + box[2] - 1
                x_samples = [start_x, centre[0], end_x]

        for x_coord in x_samples:
            relay_id = int(x_coord / lane_width)
            relay_id = max(0, min(relay_id, relay_num - 1))
            fired.add(relay_id)

    return sorted(fired)
