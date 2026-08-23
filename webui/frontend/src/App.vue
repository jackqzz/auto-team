<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import AdminLayout from '@/layouts/AdminLayout.vue'

const route = useRoute()
const publicPage = computed(() => !!route.meta?.public)
</script>

<template>
  <el-config-provider :locale="zhCn">
    <router-view v-if="publicPage" />
    <AdminLayout v-else />
  </el-config-provider>
</template>
