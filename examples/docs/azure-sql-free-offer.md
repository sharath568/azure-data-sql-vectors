# Keeping Azure SQL Database free

The Azure SQL Database free offer gives each subscription up to ten General
Purpose serverless databases. Every database gets 100,000 vCore-seconds of
compute, 32 GB of data and 32 GB of backup storage each month, for the life
of the subscription.

The setting that matters is what happens when the allowance runs out. With
AutoPause (in Bicep: freeLimitExhaustionBehavior = 'AutoPause') the database
pauses until the first of next month and you pay nothing. With
BillOverUsage it keeps running and bills at normal serverless rates. You can
switch from auto-pause to billing, but not back.

## Where the vCore-seconds go

Serverless bills per second for the larger of the vCores you use and a
floor. The floor is max(min vCores, min memory / 3). For a 2 vCore database
with 0.5 min vCores the floor is about 0.68 vCores, because its minimum
memory is 2.05 GB.

The surprise for most people is the idle tail. After your last query the
database stays online, and keeps billing at the floor, until the auto-pause
delay has passed. The default delay is 60 minutes. The minimum for General
Purpose is 15 minutes. Setting the delay to 15 minutes is the single
cheapest change you can make for a demo database: it cuts the idle tail of
every session by three quarters.

Stop paying for the database when you are not using it by letting it pause.
Nothing needs to run on a schedule; it pauses by itself.
