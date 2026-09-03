<script setup>
import { Icon } from '@iconify/vue'

// 表单项的补充说明：短提示留在控件旁，完整说明收进这个 ⓘ 悬停气泡。
// 「全自动批量」页有十几处开关，每处都把长说明平铺在下方会让页面长到难以扫读。
defineProps({
  // 风险类说明用 warning 着色，让不可逆操作的图标本身就能被一眼注意到
  type: { type: String, default: 'info' },
})
</script>

<template>
  <el-tooltip placement="top" :show-after="150" effect="dark" popper-class="field-hint-popper">
    <template #content>
      <div class="field-hint-content">
        <slot />
      </div>
    </template>
    <Icon
      :icon="type === 'warning' ? 'lucide:alert-triangle' : 'lucide:info'"
      class="field-hint-icon"
      :class="type === 'warning' ? 'is-warning' : ''"
    />
  </el-tooltip>
</template>

<style scoped>
.field-hint-icon {
  font-size: 14px;
  color: var(--el-text-color-placeholder);
  cursor: help;
  vertical-align: -2px;
  transition: color 0.15s;
}

.field-hint-icon:hover {
  color: var(--el-color-primary);
}

.field-hint-icon.is-warning {
  color: var(--el-color-warning);
}
</style>

<style>
/* 气泡限宽，否则长说明会拉成一整行横穿屏幕 */
.field-hint-popper.el-popper {
  max-width: 340px;
  line-height: 1.7;
}

.field-hint-content {
  font-size: 12px;
}
</style>
