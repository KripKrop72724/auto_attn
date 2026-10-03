# Uses the caller's Invoke-Docker/Get-DatabaseRevision helpers and Compose scope.
# This database has a generated name and is never attached to an ADD worker.
function Assert-DatabaseBackupRestorable {
    param(
        [Parameter(Mandatory = $true)][string] $DatabaseUser,
        [Parameter(Mandatory = $true)][string] $ContainerBackup,
        [AllowNull()][AllowEmptyString()][string] $ExpectedRevision
    )
    $verificationDatabase = "add_restore_verify_$([Guid]::NewGuid().ToString('N'))"
    $created = $false
    try {
        Invoke-Docker -Arguments ($compose + @(
            "exec", "-T", "postgres", "createdb", "-U", $DatabaseUser,
            "--template=template0", $verificationDatabase
        )) -Capture | Out-Null
        $created = $true
        # Capture diagnostic text: pg_restore failures may include protected
        # row values. The caller receives only the failed command/exit code.
        Invoke-Docker -Arguments ($compose + @(
            "exec", "-T", "postgres", "pg_restore", "-U", $DatabaseUser,
            "--exit-on-error", "--no-owner", "--dbname", $verificationDatabase,
            $ContainerBackup
        )) -Capture | Out-Null
        $actualRevision = Get-DatabaseRevision -DatabaseUser $DatabaseUser -DatabaseName $verificationDatabase
        if ($ExpectedRevision -and $actualRevision -cne $ExpectedRevision) {
            throw "Restored backup schema revision does not match the pre-deployment revision."
        }
    } finally {
        if ($created) {
            Invoke-Docker -Arguments ($compose + @(
                "exec", "-T", "postgres", "dropdb", "-U", $DatabaseUser,
                "--if-exists", $verificationDatabase
            )) -Capture | Out-Null
        }
    }
    return [DateTime]::UtcNow.ToString("o")
}
