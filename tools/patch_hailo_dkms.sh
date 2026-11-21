#!/usr/bin/env bash
# Apply the tested mmap_read_lock fix to the hailo_pci 4.23.0 DKMS driver and rebuild it.
#
# Source of the fix:
#   https://community.hailo.ai/t/hailo-pci-find-vma-called-without-mmap-lock-in-hailo-vdma-buffer-map-kernel-warning-storm-and-hard-crash-under-sustained-inference/19635
# Same fix in the Raspberry Pi in-kernel copy of the driver:
#   https://github.com/raspberrypi/linux/commit/2d3b74b83db7e045767ba5abfeff9a682dc8619c
#
# Usage:  sudo ./patch_hailo_dkms.sh
# Then:   sudo reboot

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "run with sudo" >&2
    exit 1
fi

read -r MODULE VERSION < <(dkms status | awk -F'[/, ]' '/hailo/ {print $1, $2; exit}')
if [[ -z "${MODULE:-}" || -z "${VERSION:-}" ]]; then
    echo "no hailo module found in dkms status" >&2
    exit 1
fi
echo "dkms module: $MODULE/$VERSION"

PATCH=$(mktemp)
cat > "$PATCH" <<'DIFF'
--- a/linux/vdma/memory.c
+++ b/linux/vdma/memory.c
@@ -167,11 +167,13 @@
     }
 
     if (HAILO_DMA_DMABUF_BUFFER != buffer_type) {
+        mmap_read_lock(current->mm);
         vma = find_vma(current->mm, addr_or_fd);
         if (IS_ENABLED(HAILO_SUPPORT_MMIO_DMA_MAPPING)) {
             if (NULL == vma) {
                 dev_err(dev, "no vma for virt_addr/size = 0x%08lx/0x%08zx\n", addr_or_fd, size);
                 ret = -EFAULT;
+                mmap_read_unlock(current->mm);
                 goto cleanup;
             }
         }
@@ -182,12 +184,14 @@
             ret = create_fd_from_vma(dev, vma);
             if (ret < 0) {
                 dev_err(dev, "Failed creating fd from vma in given dmabuf\n");
+                mmap_read_unlock(current->mm);
                 goto cleanup;
             }
             // Save original dmabuf user address in dmabuf_from_pointer_addr and override addr_or_fd with fd
             dmabuf_from_pointer_addr = addr_or_fd;
             addr_or_fd = ret;
         }
+        mmap_read_unlock(current->mm);
     }
 
     // TODO: is MMIO DMA MAPPINGS STILL needed after dmabuf
DIFF

apply_patch() {
    local dir="$1"
    local f="$dir/linux/vdma/memory.c"
    [[ -f "$f" ]] || return 0

    if grep -q "mmap_read_lock(current->mm);" <(sed -n '160,200p' "$f"); then
        echo "already patched: $f"
        return 0
    fi

    # Dry run first so a mismatched source fails before anything is touched
    if ! patch -p1 -d "$dir" --dry-run --fuzz=2 < "$PATCH" >/dev/null; then
        echo "patch does not apply cleanly to $f" >&2
        echo "the source differs from the 4.23.0 layout the fix was written for; not touching it" >&2
        return 1
    fi
    cp "$f" "$f.orig"
    patch -p1 -d "$dir" --fuzz=2 < "$PATCH"
    echo "patched: $f (backup at $f.orig)"
    echo "---- patched region ----"
    sed -n '165,200p' "$f"
    echo "------------------------"
}

apply_patch "/usr/src/${MODULE}-${VERSION}"
if [[ -d /usr/src/hailort-pcie-driver && ! -L /usr/src/hailort-pcie-driver ]]; then
    apply_patch /usr/src/hailort-pcie-driver
fi
rm -f "$PATCH"

echo "rebuilding module..."
dkms build "${MODULE}/${VERSION}" --force
dkms install "${MODULE}/${VERSION}" --force

# Hold every Hailo package that ships the driver source or must stay version-matched to it
HOLD=$(dpkg-query -W -f='${Package}\n' 'hailo*' 'python3-hailort' 2>/dev/null | grep -E '^(hailort-pcie-driver|hailo-dkms|hailort|hailo-tappas-core|python3-hailort)$' || true)
if [[ -n "$HOLD" ]]; then
    apt-mark hold $HOLD >/dev/null && echo "held: $(echo $HOLD)"
else
    echo "warning: no hailo packages found to hold; run dpkg -l | grep -i hailo and hold the driver package manually" >&2
fi

echo
echo "done. reboot, then:"
echo "  hailortcli benchmark /usr/share/hailo-models/yolov8s_h8.hef"
echo "  dmesg | grep -c rwsem      # should be 0"
