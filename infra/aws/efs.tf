resource "aws_efs_file_system" "uploads" {
  creation_token = local.name
  encrypted      = true

  # Source documents are written once and read again during ingestion; moving them to
  # Infrequent Access after 30 days keeps the storage line small.
  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = { Name = "${local.name}-uploads" }
}

# A mount target per private subnet: a Fargate task can only mount EFS in the AZs that
# have one, and the tasks are spread across both.
resource "aws_efs_mount_target" "uploads" {
  count           = var.az_count
  file_system_id  = aws_efs_file_system.uploads.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "uploads" {
  file_system_id = aws_efs_file_system.uploads.id

  # The image runs as the non-root user `app` (created with `useradd --system`, so its UID
  # is not fixed at build time). The access point performs file operations as this POSIX
  # identity inside a root directory it owns, which is what lets a non-root container write
  # to EFS without pinning a UID. Tighten it by setting efs_posix_uid/gid and
  # efs_root_permissions in terraform.tfvars to the image's real UID
  # (`docker run --rm --entrypoint id <image> app`).
  posix_user {
    uid = var.efs_posix_uid
    gid = var.efs_posix_gid
  }

  root_directory {
    path = "/uploads"

    creation_info {
      owner_uid   = var.efs_posix_uid
      owner_gid   = var.efs_posix_gid
      permissions = var.efs_root_permissions
    }
  }

  tags = { Name = "${local.name}-uploads" }
}
